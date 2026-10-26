"""Govee LAN bridge: per-section and per-segment control of Govee lights from Home Assistant, LAN only.

Lights are discovered from govee2mqtt's HTTP API (`/api/devices`) and recorded in a config file
(`devices.toml`) that the bridge keeps up to date. Each light with a known profile gets Home Assistant
entities through MQTT discovery; commands go to the light as Govee LAN `ptReal` frames (UDP 4003).
Standard library only, so nothing is downloaded at runtime.

Profiles
  sections  Govee H60B0 Uplighter: whole lamp, Ripple / Side / Lower sections, 8 side segments, flow rate
  segments  addressable lights using the 33 05 15 command family (H6088 cube sconces, H61E0 / H612D light strips):
            whole lamp plus one light per segment; zoned ones (H60A6 Ceiling Light Pro, experimental) also
            get a light per zone, switched on its own

The lights cannot report per-section or per-segment state over LAN, so state is what the bridge last set
(retained in MQTT so it survives restarts).
"""

import base64
import json
import math
import os
import re
import socket
import struct
import threading
import time
import tomllib
import urllib.request

MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER") or None
MQTT_PASS = os.environ.get("MQTT_PASS") or None
DISCOVERY = os.environ.get("DISCOVERY_PREFIX", "homeassistant")
GOVEE2MQTT_URL = os.environ.get("GOVEE2MQTT_URL", "http://127.0.0.1:8056").rstrip("/")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/devices.toml")
REFRESH = int(os.environ.get("DISCOVERY_INTERVAL", "600"))      # seconds between govee2mqtt refreshes

ROOT = "govee_lan"
STATUS = f"{ROOT}/status"
LAMP_PORT = 4003
MIN_K, MAX_K = 2700, 6500          # outside this range the H60B0 blacks out; safe for all profiles
MIN_GAP = 0.15                     # seconds a light is busy per command; packets arriving sooner can be dropped
MAX_FRAMES = int(os.environ.get("MAX_FRAMES_PER_PACKET", "12"))  # lights drop packets of ~24 frames
BATCH_WINDOW = float(os.environ.get("BATCH_WINDOW", "0.15"))     # commands this close together form one change

# Known models. Users can add others in devices.toml with `family` (and `segments`).
PROFILES = {
    "H60B0": {"family": "sections"},
    "H6088": {"family": "segments", "segments": 6},
    "H61E0": {"family": "segments", "segments": 15},
    "H612D": {"family": "segments", "segments": 20},
    # Ceiling Light Pro: a main light and a background ring, each switched on its own. Mapped from the
    # app-derived device data in Brady-Woods/govee-ble-local (MIT); not yet verified over LAN.
    "H60A6": {"family": "segments", "segments": 13, "kelvin": "tint",
              "zones": [("main", "Main light", 0, [13]), ("ring", "Ring", 1, list(range(1, 13)))]},
}


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


# ---------------------------------------------------------------- protocol helpers

def frame(*data):
    """20-byte Govee frame: data, zero padding, XOR checksum."""
    b = list(data) + [0] * (19 - len(data))
    x = 0
    for v in b:
        x ^= v
    return bytes(b + [x])


def kelvin_to_rgb(k):
    """Approximate RGB for a colour temperature (the app sends one alongside the Kelvin value)."""
    t = k / 100.0
    if t <= 66:
        r = 255
        g = 99.4708025861 * math.log(t) - 161.1195681661
        b = 0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    else:
        r = 329.698727446 * ((t - 60) ** -0.1332047592)
        g = 288.1221695283 * ((t - 60) ** -0.0755148492)
        b = 255
    return tuple(max(0, min(255, int(c))) for c in (r, g, b))


def clamp_k(k):
    return max(MIN_K, min(MAX_K, int(k)))


def pct(brightness_255):
    return max(1, min(100, round(int(brightness_255) * 100 / 255)))


def mask_bytes(mask, width):
    return tuple((mask >> (8 * i)) & 0xFF for i in range(width))


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "govee"


# Shared by every Govee light
def f_power(on):            return frame(0x33, 0x01, 1 if on else 0)
def f_master(p):            return frame(0x33, 0x04, p)

# H60B0 "sections" family
SECTIONS = {"ripple": 1, "side": 2, "lower": 3}
SIDE_SEGMENTS = 8                  # side ring; mask bit n = segment n+1
SIDE_OFF_PCT = 1                   # diffuse ring: minimum brightness is as dark as it gets


def f_section(sec, on):     return frame(0x33, 0x30, sec, 1 if on else 0)
def f_flow(v):              return frame(0x33, 0x05, 0x2C, 0x01, 0x03, max(0, min(100, int(v))))


def f_brightness(sec, p, mask=0xFF):
    if sec == 2:
        return frame(0x33, 0x05, 0x2C, 2, 0x02, p, mask)   # side needs the segment mask
    return frame(0x33, 0x05, 0x2C, sec, 0x02, p)


def f_rgb(sec, r, g, b, mask=0xFF):
    if sec == 3:
        raise ValueError("lower light has no colour")       # colour bytes black out the lower light
    extra = (mask,) if sec == 2 else ()
    return frame(0x33, 0x05, 0x2C, sec, 0x01, r, g, b, 0, 0, *extra)


def f_kelvin(sec, k, mask=0xFF):
    k = clamp_k(k)
    if sec == 3:
        return frame(0x33, 0x05, 0x2C, 3, 0x01, k >> 8, k & 0xFF)   # no RGB bytes for lower
    r, g, b = kelvin_to_rgb(k)
    extra = (mask,) if sec == 2 else ()
    return frame(0x33, 0x05, 0x2C, sec, 0x01, r, g, b, k >> 8, k & 0xFF, *extra)


# "segments" family (33 05 15): colour/kelvin mask at byte 12, brightness mask right after the value
def f_seg_rgb(r, g, b, mask, width):
    return frame(0x33, 0x05, 0x15, 0x01, r, g, b, 0, 0, 0, 0, 0, *mask_bytes(mask, width))


def f_seg_kelvin(k, mask, width):
    k = clamp_k(k)
    return frame(0x33, 0x05, 0x15, 0x01, 0, 0, 0, k >> 8, k & 0xFF, 0, 0, 0, *mask_bytes(mask, width))


def f_seg_kelvin_tint(k, mask, width):
    """Segment colour temperature as the H60A6 takes it: ff ff ff, the Kelvin value, then its tint colour."""
    k = clamp_k(k)
    return frame(0x33, 0x05, 0x15, 0x01, 0xFF, 0xFF, 0xFF, k >> 8, k & 0xFF, *kelvin_to_rgb(k),
                 *mask_bytes(mask, width))


def f_seg_brightness(p, mask, width):
    return frame(0x33, 0x05, 0x15, 0x02, p, *mask_bytes(mask, width))


def f_zone(index, on):      return frame(0x33, 0x30, index, 1 if on else 0)   # zoned segment lights (H60A6)


class Lamp:
    """Sends commands to one light in order, at least MIN_GAP apart.

    Each command keeps the light busy for ~MIN_GAP, and a datagram arriving while it is busy can be silently
    dropped: ptReal after ptReal, ptReal right after standard commands, and standard commands right after
    a ptReal datagram were all seen to be lost. ptReal frames go several per datagram (see MAX_FRAMES)."""

    def __init__(self, ip):
        self.ip = ip
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.lock = threading.Lock()
        self.last = 0.0

    def _transmit(self, msg):
        with self.lock:
            wait = self.last + MIN_GAP - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.sock.sendto(json.dumps(msg).encode(), (self.ip, LAMP_PORT))
            self.last = time.monotonic()

    def command(self, cmd, data):
        """A standard (documented) LAN command."""
        self._transmit({"msg": {"cmd": cmd, "data": data}})
        log(f"-> {self.ip}: {cmd} {json.dumps(data)}")

    def send(self, frames):
        for i in range(0, len(frames), MAX_FRAMES):
            chunk = frames[i:i + MAX_FRAMES]
            self._transmit({"msg": {"cmd": "ptReal", "data": {"command": [base64.b64encode(f).decode() for f in chunk]}}})
            log(f"-> {self.ip}: {' | '.join(f.hex(' ') for f in chunk)}")


# ---------------------------------------------------------------- minimal MQTT 3.1.1 client

def _str(s):
    b = s.encode()
    return struct.pack("!H", len(b)) + b


def _remaining(n):
    out = bytearray()
    while True:
        d, n = n % 128, n // 128
        out.append(d | (0x80 if n else 0))
        if not n:
            return bytes(out)


class MQTT:
    def __init__(self, on_message):
        self.on_message = on_message
        self.sock = None
        self.wlock = threading.Lock()
        self.pid = 0

    def _send(self, ptype, body):
        with self.wlock:
            self.sock.sendall(bytes([ptype]) + _remaining(len(body)) + body)

    def connect(self, will_topic, will_msg):
        self.sock = socket.create_connection((MQTT_HOST, MQTT_PORT), timeout=10)
        flags = 0x02 | 0x04 | 0x20          # clean session, will flag, will retain (QoS 0)
        payload = _str("govee-lan-bridge") + _str(will_topic) + _str(will_msg)
        if MQTT_USER:
            flags |= 0x80
            payload += _str(MQTT_USER)
            if MQTT_PASS:
                flags |= 0x40
                payload += _str(MQTT_PASS)
        self._send(0x10, _str("MQTT") + bytes([4, flags]) + struct.pack("!H", 60) + payload)
        ptype, body = self._read()
        if ptype != 0x20 or body[1] != 0:
            raise ConnectionError(f"MQTT connect refused (code {body[1] if len(body) > 1 else '?'})")
        self.sock.settimeout(None)

    def subscribe(self, topic):
        self.pid = self.pid % 65535 + 1
        self._send(0x82, struct.pack("!H", self.pid) + _str(topic) + b"\x00")

    def publish(self, topic, payload, retain=False):
        if not isinstance(payload, (bytes, bytearray)):
            payload = (payload if isinstance(payload, str) else json.dumps(payload)).encode()
        self._send(0x30 | (1 if retain else 0), _str(topic) + payload)

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("MQTT connection closed")
            buf += chunk
        return buf

    def _read(self):
        ptype = self._recv_exact(1)[0]
        mult, length = 1, 0
        while True:
            d = self._recv_exact(1)[0]
            length += (d & 0x7F) * mult
            mult *= 128
            if not d & 0x80:
                break
        return ptype, self._recv_exact(length) if length else b""

    def loop(self):
        """Read packets until the connection drops; a helper thread sends keep-alive pings."""
        stop = threading.Event()

        def pinger():
            while not stop.wait(30):
                try:
                    self._send(0xC0, b"")
                except OSError:
                    return
        threading.Thread(target=pinger, daemon=True).start()
        try:
            while True:
                ptype, body = self._read()
                if ptype >> 4 == 3:                       # PUBLISH
                    tlen = struct.unpack("!H", body[:2])[0]
                    topic = body[2:2 + tlen].decode()
                    off = 2 + tlen + (2 if (ptype >> 1) & 3 else 0)
                    try:
                        self.on_message(topic, body[off:], bool(ptype & 1))
                    except Exception as e:                # never let one bad message kill the bridge
                        log(f"error handling {topic}: {e!r}")
        finally:
            stop.set()
            try:
                self.sock.close()
            except OSError:
                pass


# ---------------------------------------------------------------- inventory (govee2mqtt + devices.toml)

CONFIG_HEADER = """# govee-lan-bridge devices
#
# Maintained by the bridge: every light govee2mqtt reports is added here automatically and IP addresses
# are kept up to date. Your edits are kept: change `key` (entity ID prefix) or `name`, set `enabled`, or
# describe a model the bridge doesn't know yet with `family` ("segments" or "sections") and `segments`.
# If govee2mqtt's API is unavailable, the bridge runs from this file.
"""


def toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    return json.dumps(str(v))


def toml_block(entry, note=""):
    lines = ["", "[[device]]"]
    for k in ("id", "sku", "ip", "key", "name", "room", "enabled", "family", "segments"):
        if k in entry and entry[k] not in (None, ""):
            lines.append(f"{k} = {toml_value(entry[k])}")
    if note:
        lines.append(f"# {note}")
    return "\n".join(lines) + "\n"


class Inventory:
    """Merges govee2mqtt's device list into devices.toml and returns the lights to bridge."""

    def __init__(self, path=CONFIG_PATH, url=GOVEE2MQTT_URL):
        self.path, self.url = path, url

    def load(self):
        try:
            with open(self.path, "rb") as f:
                return tomllib.load(f).get("device", [])
        except FileNotFoundError:
            return []

    def fetch(self):
        with urllib.request.urlopen(f"{self.url}/api/devices", timeout=10) as r:
            return json.load(r)

    def sync(self, discovered=None):
        """Update the config file from govee2mqtt (if reachable) and return the entries."""
        entries = self.load()
        if discovered is None:
            try:
                discovered = self.fetch()
            except Exception as e:
                log(f"govee2mqtt API unavailable ({e!r}); using {self.path}")
                discovered = []
        text = open(self.path, encoding="utf8").read() if os.path.exists(self.path) else CONFIG_HEADER
        changed = False
        by_id = {e.get("id"): e for e in entries if e.get("id")}
        for d in discovered:
            did, sku, ip = d.get("id"), (d.get("sku") or "").upper(), d.get("ip")
            if not did:
                continue
            if did in by_id:
                e = by_id[did]
                if ip and e.get("ip") != ip:              # keep the recorded IP current
                    text = self.replace_ip(text, did, ip)
                    e["ip"], changed = ip, True
                continue
            entry = {"id": did, "sku": sku, "ip": ip, "name": d.get("name") or sku, "room": d.get("room") or ""}
            if sku in PROFILES:
                entry["enabled"] = True
                note = ""
            else:
                entry["enabled"] = False
                note = "no built-in profile: add family/segments and set enabled = true to bridge it"
            entry["key"] = self.unique_key(entry, entries)
            text += toml_block(entry, note)
            entries.append(entry)
            by_id[did] = entry
            changed = True
            log(f"discovered {sku} {did} at {ip} -> {'bridged' if entry['enabled'] else 'recorded (no profile)'}")
        if changed:
            self.write(text)
        return entries

    @staticmethod
    def unique_key(entry, entries):
        """Entity ID prefix: the Govee room plus the device name if a room is set, else the device name;
        suffixed if another light already uses it."""
        base = slug(f"{entry['room']} {entry['name']}" if entry.get("room") else entry["name"])
        taken = {e.get("key") for e in entries}
        key, n = base, 2
        while key in taken:
            key, n = f"{base}_{n}", n + 1
        return key

    @staticmethod
    def replace_ip(text, did, ip):
        blocks = re.split(r"(?m)^(?=\[\[device\]\])", text)
        for i, block in enumerate(blocks):
            if re.search(rf'(?m)^id\s*=\s*"{re.escape(did)}"', block):
                if re.search(r"(?m)^ip\s*=", block):
                    blocks[i] = re.sub(r'(?m)^ip\s*=.*$', f"ip = {json.dumps(ip)}", block)
                else:
                    blocks[i] = block.replace("[[device]]\n", f"[[device]]\nip = {json.dumps(ip)}\n", 1)
        return "".join(blocks)

    def write(self, text):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf8") as f:
            f.write(text)
        os.replace(tmp, self.path)

    @staticmethod
    def profile(entry):
        """Profile for an entry (config overrides built-in), or None if it can't be bridged."""
        prof = dict(PROFILES.get((entry.get("sku") or "").upper(), {}))
        for k in ("family", "segments"):
            if entry.get(k):
                prof[k] = entry[k]
        if prof.get("family") == "sections" or (prof.get("family") == "segments" and int(prof.get("segments", 0)) > 0):
            return prof
        return None


# ---------------------------------------------------------------- devices

class Device:
    """One light: owns its state, batching and UDP sender. Subclasses implement the family."""

    model = "Govee light"

    def __init__(self, key, name, ip, sku, mqtt, room=""):
        self.key, self.name, self.sku, self.mqtt, self.room = key, name, sku, mqtt, room
        self.base = f"{ROOT}/{key}"
        self.lamp = Lamp(ip)
        self.pending, self.plock, self.timer = {}, threading.Lock(), None
        self.commands = []            # standard LAN commands for the current change

    def device_info(self):
        info = {"identifiers": [f"govee_lan_{self.key}"], "name": self.name,
                "manufacturer": "Govee", "model": f"{self.sku} (LAN bridge)"}
        if self.room:
            info["suggested_area"] = self.room
        return info

    def light_config(self, entity, label, modes, unique=None):
        uid = unique or f"{self.key}_{entity}"
        return {"name": label, "unique_id": uid, "object_id": uid, "schema": "json",
                "command_topic": f"{self.base}/{entity}/set", "state_topic": f"{self.base}/{entity}/state",
                "brightness": True, "supported_color_modes": modes, "color_temp_kelvin": True,
                "min_kelvin": MIN_K, "max_kelvin": MAX_K, "device": self.device_info(),
                "availability_topic": STATUS}

    def publish_light(self, entity, label, modes):
        self.mqtt.publish(f"{DISCOVERY}/light/{self.key}/{entity}/config",
                          self.light_config(entity, label, modes), retain=True)

    @staticmethod
    def light_payload(s, on):
        out = {"state": "ON" if on else "OFF", "brightness": s["brightness"], "color_mode": s["color_mode"]}
        if s["color_mode"] == "color_temp":
            out["color_temp"] = s["color_temp"]
        else:
            out["color"] = s["color"]
        return out

    @staticmethod
    def apply_colour(target, cmd):
        """Copy colour/temperature/brightness from an HA command into a state dict; True if colour changed."""
        changed = False
        if "color" in cmd:
            target["color"], target["color_mode"] = {k: int(cmd["color"][k]) for k in "rgb"}, "rgb"
            changed = True
        elif "color_temp" in cmd:
            target["color_temp"], target["color_mode"] = clamp_k(cmd["color_temp"]), "color_temp"
            changed = True
        if "brightness" in cmd:
            target["brightness"] = int(cmd["brightness"])
        return changed

    # -- batching ---------------------------------------------------------
    def queue(self, entity, value):
        """Collect commands arriving close together (e.g. a scene) and apply them as one change."""
        with self.plock:
            if isinstance(value, dict) and entity in self.pending:
                self.pending[entity].update(value)
            else:
                self.pending[entity] = value
            if self.timer is None:
                self.timer = threading.Timer(BATCH_WINDOW, self.flush)
                self.timer.start()

    def flush(self):
        with self.plock:
            pending, self.pending, self.timer = self.pending, {}, None
        self.commands = []
        frames = self.apply(pending)
        unique = []
        for f in frames:                                  # drop repeated frames (e.g. power on)
            if f not in unique:
                unique.append(f)
        for cmd, data in self.commands:                   # standard commands first, then the ptReal (Lamp spaces them)
            self.lamp.command(cmd, data)
        self.lamp.send(unique)                            # then one datagram for all ptReal frames
        self.publish_states()

    # -- MQTT dispatch ------------------------------------------------------------
    def handle(self, entity, kind, payload, retained, restoring):
        if kind == "state":
            if restoring and retained:
                self.restore(entity, payload)
            return
        if kind == "set" and entity in self.entities():
            self.queue(entity, self.parse(entity, payload))

    def parse(self, entity, payload):
        return json.loads(payload)


class SegmentDevice(Device):
    """Lights using the 33 05 15 family: whole lamp + N segments; segment off = black.

    Whole-lamp power, brightness and colour use the standard LAN commands (documented, reported by the
    light's own status, and never dropped when sent back-to-back); ptReal is only used for segments.
    Zoned lights (H60A6) also get a light per zone: each zone is switched on its own (33 30 <index>) and
    coloured through its segments."""

    def power(self, on):
        self.commands.append(("turn", {"value": 1 if on else 0}))

    def master(self, p):
        self.commands.append(("brightness", {"value": p}))

    def whole_colour(self):
        w = self.whole
        if w["color_mode"] == "color_temp":
            self.commands.append(("colorwc", {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": clamp_k(w["color_temp"])}))
        else:
            self.commands.append(("colorwc", {"color": dict(w["color"]), "colorTemInKelvin": 0}))

    def __init__(self, key, name, ip, sku, mqtt, segments, room="", zones=(), kelvin="plain"):
        super().__init__(key, name, ip, sku, mqtt, room)
        self.n = int(segments)
        self.width = (self.n + 7) // 8
        self.all = (1 << self.n) - 1
        self.seg_kelvin = f_seg_kelvin_tint if kelvin == "tint" else f_seg_kelvin
        self.whole = {"state": "OFF", "brightness": 255, "color_mode": "color_temp", "color_temp": 4000,
                      "color": {"r": 255, "g": 200, "b": 150}}
        self.segs = [{"on": True, "brightness": 255, "color_mode": "color_temp", "color_temp": 4000,
                      "color": {"r": 255, "g": 200, "b": 150}} for _ in range(self.n)]
        self.zones = [{"key": zkey, "label": label, "index": index, "members": [m - 1 for m in members],
                       "state": "ON", "brightness": 255, "color_mode": "color_temp", "color_temp": 4000,
                       "color": {"r": 255, "g": 200, "b": 150}} for zkey, label, index, members in zones]
        self.zone_of = {i: z for z in self.zones for i in z["members"]}
        # A segment that makes up a whole zone has no light of its own: the zone's light covers it
        self.seg_entities = [i for i in range(self.n) if i not in self.zone_of or len(self.zone_of[i]["members"]) > 1]

    def entities(self):
        return ["lamp"] + [z["key"] for z in self.zones] + [f"segment_{i + 1}" for i in self.seg_entities]

    def discover(self):
        self.publish_light("lamp", "Lamp", ["rgb", "color_temp"])
        for z in self.zones:
            self.publish_light(z["key"], z["label"], ["rgb", "color_temp"])
        for i in self.seg_entities:
            self.publish_light(f"segment_{i + 1}", f"Segment {i + 1}", ["rgb", "color_temp"])

    def publish_states(self):
        on = self.whole["state"] == "ON"
        self.mqtt.publish(f"{self.base}/lamp/state", self.light_payload(self.whole, on), retain=True)
        for z in self.zones:
            out = self.light_payload(z, on and z["state"] == "ON")
            out["zone_on"] = z["state"] == "ON"
            self.mqtt.publish(f"{self.base}/{z['key']}/state", out, retain=True)
        for i in self.seg_entities:
            seg, zone = self.segs[i], self.zone_of.get(i)
            out = self.light_payload(seg, on and seg["on"] and (zone is None or zone["state"] == "ON"))
            out["segment_on"] = seg["on"]
            self.mqtt.publish(f"{self.base}/segment_{i + 1}/state", out, retain=True)

    def zone(self, entity):
        return next((z for z in self.zones if z["key"] == entity), None)

    def restore(self, entity, payload):
        data = json.loads(payload)
        if entity == "lamp":
            self.whole.update(data)
        elif (z := self.zone(entity)) is not None:
            z.update({k: data[k] for k in ("brightness", "color_mode", "color", "color_temp") if k in data})
            z["state"] = "ON" if data.get("zone_on", data.get("state") == "ON") else "OFF"
        elif entity.startswith("segment_"):
            i = int(entity.split("_")[1]) - 1
            if 0 <= i < self.n:
                self.segs[i].update({k: data[k] for k in ("brightness", "color_mode", "color", "color_temp") if k in data})
                self.segs[i]["on"] = data.get("segment_on", data.get("state") == "ON")

    def segment_frames(self, indices):
        """Frames applying the remembered settings of the given segments; shared settings share a frame."""
        colours, levels = {}, {}
        for i in indices:
            seg, bit = self.segs[i], 1 << i
            if not seg["on"]:
                key = ("rgb", 0, 0, 0)                     # black is a true per-segment off
            elif seg["color_mode"] == "color_temp":
                key = ("k", seg["color_temp"])
            else:
                key = ("rgb", seg["color"]["r"], seg["color"]["g"], seg["color"]["b"])
            colours[key] = colours.get(key, 0) | bit
            if seg["on"]:
                p = pct(seg["brightness"])
                levels[p] = levels.get(p, 0) | bit
        frames = [self.seg_kelvin(k[1], m, self.width) if k[0] == "k" else f_seg_rgb(*k[1:], m, self.width)
                  for k, m in colours.items()]
        frames += [f_seg_brightness(p, m, self.width) for p, m in levels.items()]
        return frames

    def zone_frames(self):
        """Every zone's remembered on/off: what the light brings back by itself isn't known."""
        if self.zones and not any(z["state"] == "ON" for z in self.zones):
            for z in self.zones:
                z["state"] = "ON"
        return [f_zone(z["index"], z["state"] == "ON") for z in self.zones]

    def light_lamp(self):
        """Power the lamp on (if it's off), with every zone as remembered."""
        if self.whole["state"] == "ON":
            return []
        self.whole["state"] = "ON"
        self.power(True)
        self.master(pct(self.whole["brightness"]))
        return self.zone_frames()

    def zone_apply(self, z, cmd):
        if cmd.get("state") == "OFF":
            z["state"] = "OFF"
            return [f_zone(z["index"], False)]
        frames = self.light_lamp()
        z["state"] = "ON"
        frames.append(f_zone(z["index"], True))       # always: the zone's state is only assumed
        members = [self.segs[i] for i in z["members"]]
        colour_changed = self.apply_colour(z, cmd)
        if not any(seg["on"] for seg in members):
            for seg in members:
                seg["on"] = True
        if colour_changed or "brightness" in cmd:      # a zone's colour or brightness repaints its segments
            for seg in members:
                seg["on"] = True
                if colour_changed:
                    seg["color_mode"], seg["color"], seg["color_temp"] = (
                        z["color_mode"], dict(z["color"]), z["color_temp"])
                if "brightness" in cmd:
                    seg["brightness"] = z["brightness"]
            frames += self.segment_frames(z["members"])
        return frames

    def apply(self, pending):
        frames = []
        cmd = pending.get("lamp")
        seg_cmds = {int(e.split("_")[1]) - 1: c for e, c in pending.items() if e.startswith("segment_")}
        zone_cmds = [(z, pending[z["key"]]) for z in self.zones if z["key"] in pending]
        if cmd is not None:
            if cmd.get("state") == "OFF":
                self.whole["state"] = "OFF"
                self.power(False)
                return []
            turning_on = self.whole["state"] != "ON"
            self.whole["state"] = "ON"
            if turning_on:
                self.power(True)
                frames += self.zone_frames()
            if "brightness" in cmd:
                self.whole["brightness"] = int(cmd["brightness"])
                self.master(pct(self.whole["brightness"]))
            elif turning_on:
                self.master(pct(self.whole["brightness"]))
            colour = {k: v for k, v in cmd.items() if k in ("color", "color_temp")}
            if colour:                                    # whole-lamp colour repaints every segment
                self.apply_colour(self.whole, colour)
                for part in self.segs + self.zones:
                    part["color_mode"], part["color"], part["color_temp"] = (
                        self.whole["color_mode"], dict(self.whole["color"]), self.whole["color_temp"])
                for seg in self.segs:
                    seg["on"] = True
                self.whole_colour()
            elif turning_on and not seg_cmds and not zone_cmds:
                if not any(seg["on"] for seg in self.segs):
                    for seg in self.segs:
                        seg["on"] = True
                frames += self.segment_frames(range(self.n))   # restore the saved pattern
        for z, c in zone_cmds:
            frames += self.zone_apply(z, c)
        if seg_cmds:
            for i, c in seg_cmds.items():               # lighting a segment lights its zone
                zone = self.zone_of.get(i)
                if zone is not None and 0 <= i < self.n and c.get("state") != "OFF":
                    zone["state"] = "ON"
                    frames.append(f_zone(zone["index"], True))
            for i, c in seg_cmds.items():
                if not 0 <= i < self.n:
                    continue
                if c.get("state") == "OFF":
                    self.segs[i]["on"] = False
                else:
                    self.segs[i]["on"] = True
                    self.apply_colour(self.segs[i], c)
            if not any(seg["on"] for seg in self.segs):   # last segment off: switch the lamp off
                self.whole["state"] = "OFF"
                self.power(False)
                return frames
            if self.whole["state"] != "ON":
                frames += self.light_lamp()
                frames += self.segment_frames(range(self.n))
            else:
                frames += self.segment_frames(sorted(i for i in seg_cmds if 0 <= i < self.n))
        return frames


class SectionDevice(Device):
    """Govee H60B0 Uplighter: whole lamp, three sections (two on at once), 8 side segments, flow rate."""

    def __init__(self, key, name, ip, sku, mqtt, room=""):
        super().__init__(key, name, ip, sku, mqtt, room)
        self.power = {"state": "ON", "brightness": 255}
        self.state = {
            "ripple": {"state": "OFF", "brightness": 255, "color_mode": "color_temp", "color_temp": 4000, "color": {"r": 255, "g": 200, "b": 150}},
            "side":   {"state": "OFF", "brightness": 255, "color_mode": "color_temp", "color_temp": 4000, "color": {"r": 255, "g": 200, "b": 150}},
            "lower":  {"state": "OFF", "brightness": 255, "color_mode": "color_temp", "color_temp": 4000},
        }
        self.segs = [{"on": True, "brightness": 255, "color_mode": "color_temp", "color_temp": 4000,
                      "color": {"r": 255, "g": 200, "b": 150}} for _ in range(SIDE_SEGMENTS)]
        self.flow = 20
        self.on_order = []            # sections in the order they were turned on (lamp allows two)

    def entities(self):
        return ["lamp", "flow", *SECTIONS, *(f"side_segment_{i + 1}" for i in range(SIDE_SEGMENTS))]

    def parse(self, entity, payload):
        if entity == "flow":
            return max(0, min(100, int(float(payload))))
        return json.loads(payload)

    # -- discovery / state ----------------------------------------------------
    def discover(self):
        cfg = self.light_config("lamp", "Lamp", ["brightness"])
        self.mqtt.publish(f"{DISCOVERY}/light/{self.key}/lamp/config", cfg, retain=True)
        for name in SECTIONS:
            self.publish_light(name, name.capitalize(), ["color_temp"] if name == "lower" else ["rgb", "color_temp"])
        for i in range(SIDE_SEGMENTS):
            self.publish_light(f"side_segment_{i + 1}", f"Side segment {i + 1}", ["rgb", "color_temp"])
        num = {"name": "Flow rate", "unique_id": f"{self.key}_flow_rate", "object_id": f"{self.key}_flow_rate",
               "command_topic": f"{self.base}/flow/set", "state_topic": f"{self.base}/flow/state",
               "min": 0, "max": 100, "step": 1, "mode": "slider", "icon": "mdi:rotate-3d-variant",
               "device": self.device_info(), "availability_topic": STATUS}
        self.mqtt.publish(f"{DISCOVERY}/number/{self.key}/flow_rate/config", num, retain=True)

    def publish_states(self):
        powered = self.power["state"] == "ON"
        self.mqtt.publish(f"{self.base}/lamp/state", {"state": self.power["state"], "brightness": self.power["brightness"],
                                                      "color_mode": "brightness"}, retain=True)
        for name, s in self.state.items():
            self.mqtt.publish(f"{self.base}/{name}/state", self.light_payload(s, powered and s["state"] == "ON"), retain=True)
        side_on = powered and self.state["side"]["state"] == "ON"
        for i, seg in enumerate(self.segs):
            out = self.light_payload(seg, side_on and seg["on"])
            out["segment_on"] = seg["on"]
            self.mqtt.publish(f"{self.base}/side_segment_{i + 1}/state", out, retain=True)
        self.mqtt.publish(f"{self.base}/flow/state", str(self.flow), retain=True)

    def restore(self, entity, payload):
        if entity == "flow":
            self.flow = int(float(payload))
            return
        data = json.loads(payload)
        if entity == "lamp":
            self.power.update({k: data[k] for k in ("state", "brightness") if k in data})
        elif entity in self.state:
            self.state[entity].update(data)
            if data.get("state") == "ON" and entity not in self.on_order:
                self.on_order.append(entity)
        elif entity.startswith("side_segment_"):
            i = int(entity.rsplit("_", 1)[1]) - 1
            if 0 <= i < SIDE_SEGMENTS:
                self.segs[i].update({k: data[k] for k in ("brightness", "color_mode", "color", "color_temp") if k in data})
                self.segs[i]["on"] = data.get("segment_on", data.get("state") == "ON")

    # -- commands ------------------------------------------------------------
    def side_segment_frames(self, indices):
        colours, levels = {}, {}
        for i in indices:
            seg, bit = self.segs[i], 1 << i
            key = (("k", seg["color_temp"]) if seg["color_mode"] == "color_temp"
                   else ("rgb", seg["color"]["r"], seg["color"]["g"], seg["color"]["b"]))
            colours[key] = colours.get(key, 0) | bit
            p = pct(seg["brightness"]) if seg["on"] else SIDE_OFF_PCT
            levels[p] = levels.get(p, 0) | bit
        frames = [f_kelvin(2, k[1], m) if k[0] == "k" else f_rgb(2, *k[1:], mask=m) for k, m in colours.items()]
        frames += [f_brightness(2, p, m) for p, m in levels.items()]
        return frames

    def power_on_frames(self):
        if self.power["state"] == "ON":
            return [f_power(True)]
        self.power["state"] = "ON"
        return [f_power(True), f_master(pct(self.power["brightness"]))]

    def section_frames(self, name, cmd, protect=()):
        sec, s, frames = SECTIONS[name], self.state[name], []
        if cmd.get("state") == "OFF":
            s["state"] = "OFF"
            if name in self.on_order:
                self.on_order.remove(name)
            return [f_section(sec, False)]
        turning_on = s["state"] != "ON" or self.power["state"] != "ON"
        s["state"] = "ON"
        if turning_on:
            frames += self.make_room(name, protect)
        # Always (re)send power and section on: the bridge only assumes the lamp's state, and "on" to
        # a lit section is harmless, so a lamp changed elsewhere (app, power cut) gets back in step.
        frames += self.power_on_frames() + [f_section(sec, True)]
        if name in self.on_order:
            self.on_order.remove(name)
        self.on_order.append(name)
        if name == "side":
            return frames + self.side_frames(cmd, turning_on)
        colour = {k: v for k, v in cmd.items() if k in ("color", "color_temp") and not (name == "lower" and k == "color")}
        if self.apply_colour(s, colour):
            frames.append(f_kelvin(sec, s["color_temp"]) if s["color_mode"] == "color_temp"
                          else f_rgb(sec, *(s["color"][k] for k in "rgb")))
        elif turning_on:              # re-apply remembered colour so the section comes back as it was
            frames.append(f_kelvin(sec, s["color_temp"]) if s["color_mode"] == "color_temp"
                          else f_rgb(sec, *(s["color"][k] for k in "rgb")))
        if "brightness" in cmd:
            s["brightness"] = int(cmd["brightness"])
            frames.append(f_brightness(sec, pct(s["brightness"])))
        elif turning_on:
            frames.append(f_brightness(sec, pct(s["brightness"])))
        return frames

    def side_frames(self, cmd, turning_on):
        """Whole-ring command: colour/brightness repaint every segment; plain 'on' restores the pattern."""
        s = self.state["side"]
        colour_changed = self.apply_colour(s, {k: v for k, v in cmd.items() if k in ("color", "color_temp")})
        if colour_changed or "brightness" in cmd:
            if "brightness" in cmd:
                s["brightness"] = int(cmd["brightness"])
            for seg in self.segs:
                seg["on"] = True
                if colour_changed:
                    seg["color_mode"], seg["color"], seg["color_temp"] = s["color_mode"], dict(s["color"]), s["color_temp"]
                if "brightness" in cmd:
                    seg["brightness"] = s["brightness"]
            return self.side_segment_frames(range(SIDE_SEGMENTS))
        if turning_on:
            if not any(seg["on"] for seg in self.segs):
                for seg in self.segs:
                    seg["on"] = True
            return self.side_segment_frames(range(SIDE_SEGMENTS))
        return []

    def apply_segments(self, seg_cmds, protect):
        for i, cmd in seg_cmds.items():
            seg = self.segs[i]
            if cmd.get("state") == "OFF":
                seg["on"] = False
                continue
            seg["on"] = True
            self.apply_colour(seg, cmd)
        side = self.state["side"]
        if not any(seg["on"] for seg in self.segs):
            return self.section_frames("side", {"state": "OFF"}) if side["state"] == "ON" else []
        if side["state"] != "ON" or self.power["state"] != "ON":
            return self.section_frames("side", {"state": "ON"}, protect)
        # Side is believed lit; still resend power and section on in case the lamp was changed elsewhere
        return self.power_on_frames() + [f_section(2, True)] + self.side_segment_frames(sorted(seg_cmds))

    def make_room(self, name, protect=()):
        """The lamp allows two sections at once: switch off the longest-on other section,
        preferring sections that aren't being turned on in the same change."""
        on = [n for n in self.on_order if n != name and self.state[n]["state"] == "ON"]
        on.sort(key=lambda n: n in protect)
        frames = []
        while len(on) >= 2:
            victim = on.pop(0)
            self.state[victim]["state"] = "OFF"
            self.on_order.remove(victim)
            frames.append(f_section(SECTIONS[victim], False))
            log(f"{self.key}: two-section limit, turned {victim} off")
        return frames

    def apply(self, pending):
        frames = []
        lamp = pending.get("lamp")
        if lamp is not None and lamp.get("state") == "OFF":
            self.power["state"] = "OFF"
            return [f_power(False)]
        if lamp is not None:
            frames += self.power_on_frames()
            if "brightness" in lamp:
                self.power["brightness"] = int(lamp["brightness"])
                frames.append(f_master(pct(self.power["brightness"])))
        seg_cmds = {int(t.rsplit("_", 1)[1]) - 1: c for t, c in pending.items() if t.startswith("side_segment_")}
        offs = [n for n in SECTIONS if n in pending and pending[n].get("state") == "OFF"]
        ons = [n for n in SECTIONS if n in pending and n not in offs]
        protect = set(ons) | ({"side"} if any(c.get("state") != "OFF" for c in seg_cmds.values()) else set())
        for name in offs + ons:
            frames += self.section_frames(name, pending[name], protect=protect)
        if seg_cmds:
            frames += self.apply_segments({i: c for i, c in seg_cmds.items() if 0 <= i < SIDE_SEGMENTS}, protect)
        if "flow" in pending:
            self.flow = pending["flow"]
            frames.append(f_flow(self.flow))
        return frames


def make_device(entry, mqtt):
    prof = Inventory.profile(entry)
    if not prof or not entry.get("enabled", True) or not entry.get("ip"):
        return None
    key = entry.get("key") or slug(entry.get("name") or entry.get("id") or entry["ip"])
    args = (key, entry.get("name") or key, entry["ip"], (entry.get("sku") or "").upper(), mqtt)
    if prof["family"] == "sections":
        return SectionDevice(*args, room=entry.get("room") or "")
    return SegmentDevice(*args, segments=prof["segments"], room=entry.get("room") or "",
                         zones=prof.get("zones", ()), kelvin=prof.get("kelvin", "plain"))


# ---------------------------------------------------------------- bridge

class Bridge:
    def __init__(self, inventory=None):
        self.inventory = inventory or Inventory()
        self.mqtt = MQTT(self.handle)
        self.devices = {}             # key -> Device
        self.restoring = True
        self.dlock = threading.Lock()

    def load_devices(self, discovered=None):
        """(Re)build the device list from govee2mqtt + devices.toml; keeps existing instances."""
        entries = self.inventory.sync(discovered)
        added = []
        with self.dlock:
            for entry in entries:
                dev = make_device(entry, self.mqtt)
                if dev is None:
                    continue
                if dev.key in self.devices:
                    self.devices[dev.key].lamp.ip = dev.lamp.ip      # IP may have changed
                else:
                    self.devices[dev.key] = dev
                    added.append(dev)
        return added

    def handle(self, topic, payload, retained):
        parts = topic.split("/")
        if len(parts) != 4 or parts[0] != ROOT:
            return
        _, key, entity, kind = parts
        dev = self.devices.get(key)
        if dev:
            dev.handle(entity, kind, payload, retained, self.restoring)

    def refresher(self):
        while True:
            time.sleep(REFRESH)
            try:
                for dev in self.load_devices():
                    dev.discover()
                    dev.publish_states()
                    log(f"bridging new light {dev.key} ({dev.sku}) at {dev.lamp.ip}")
            except Exception as e:
                log(f"refresh failed: {e!r}")

    def run(self):
        self.load_devices()
        log("lights: " + (", ".join(f"{d.key} ({d.sku} @ {d.lamp.ip})" for d in self.devices.values()) or "none"))
        threading.Thread(target=self.refresher, daemon=True).start()
        backoff = 2
        while True:
            try:
                self.mqtt.connect(STATUS, "offline")
                log(f"connected to MQTT {MQTT_HOST}:{MQTT_PORT}")
                self.restoring = True
                for dev in list(self.devices.values()):
                    dev.discover()
                self.mqtt.subscribe(f"{ROOT}/+/+/state")
                threading.Timer(2.0, self.finish_restore).start()
                self.mqtt.publish(STATUS, "online", retain=True)
                backoff = 2
                self.mqtt.loop()
            except Exception as e:
                log(f"MQTT error: {e!r}; reconnecting in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def finish_restore(self):
        """After retained state has arrived, start listening for commands and republish state."""
        self.restoring = False
        self.mqtt.subscribe(f"{ROOT}/+/+/set")
        for dev in list(self.devices.values()):
            dev.publish_states()
        log("ready")


def drop_privileges(uid=65534, gid=65534):
    """Started as root (e.g. in Docker): make the config folder writable, then run unprivileged.
    Docker creates a missing bind-mount folder as root, so this keeps deployment hands-off."""
    if not hasattr(os, "getuid") or os.getuid() != 0:
        return
    folder = os.path.dirname(CONFIG_PATH) or "."
    os.makedirs(folder, exist_ok=True)
    for path in [folder] + [os.path.join(folder, f) for f in os.listdir(folder)]:
        os.chown(path, uid, gid)
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


if __name__ == "__main__":
    drop_privileges()
    Bridge().run()
