"""Offline tests. Frames must match commands captured from the Govee app or verified live on the lights;
bridge logic must respect each light's rules. Run: python test_bridge.py"""

import itertools
import json
import os
import tempfile

import bridge as B

fails = 0


def check(name, ok, detail=""):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n      {detail}"))


def hexf(f):
    return f.hex(" ")


# ---------------------------------------------------------------- frames vs. captures / live tests
FRAMES = {
    "H60B0 lower off":             (B.f_section(3, False), "33 30 03 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"),
    "H60B0 ripple on":             (B.f_section(1, True),  "33 30 01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 03"),
    "H60B0 lower 2700K":           (B.f_kelvin(3, 2700),   "33 05 2c 03 01 0a 8c 00 00 00 00 00 00 00 00 00 00 00 00 9e"),
    "H60B0 lower 6500K":           (B.f_kelvin(3, 6500),   "33 05 2c 03 01 19 64 00 00 00 00 00 00 00 00 00 00 00 00 65"),
    "H60B0 lower brightness 1%":   (B.f_brightness(3, 1),  "33 05 2c 03 02 01 00 00 00 00 00 00 00 00 00 00 00 00 00 1a"),
    "H60B0 ripple brightness 100": (B.f_brightness(1, 100), "33 05 2c 01 02 64 00 00 00 00 00 00 00 00 00 00 00 00 00 7d"),
    "H60B0 flow 55":               (B.f_flow(55),          "33 05 2c 01 03 37 00 00 00 00 00 00 00 00 00 00 00 00 00 2f"),
    "H60B0 ripple rgb 00ffff":     (B.f_rgb(1, 0, 255, 255), "33 05 2c 01 01 00 ff ff 00 00 00 00 00 00 00 00 00 00 00 1a"),
    "power off":                   (B.f_power(False),      "33 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 32"),
    "master brightness 100":       (B.f_master(100),       "33 04 64 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 53"),
    "H6088 cube 1 red":            (B.f_seg_rgb(255, 0, 0, 0x01, 1), "33 05 15 01 ff 00 00 00 00 00 00 00 01"),
    "H6088 cubes 1-3 2700K":       (B.f_seg_kelvin(2700, 0x07, 1), "33 05 15 01 00 00 00 0a 8c 00 00 00 07"),
    "H6088 cubes 1-3 5%":          (B.f_seg_brightness(5, 0x07, 1), "33 05 15 02 05 07 00"),
    "H61E0 segs 1-4 red":          (B.f_seg_rgb(255, 0, 0, 0x000F, 2), "33 05 15 01 ff 00 00 00 00 00 00 00 0f 00"),
    "H61E0 seg 9 (2nd mask byte)": (B.f_seg_rgb(0, 0, 255, 1 << 8, 2), "33 05 15 01 00 00 ff 00 00 00 00 00 00 01"),
    "H61E0 segs 1-3 5%":           (B.f_seg_brightness(5, 0x0007, 2), "33 05 15 02 05 07 00 00"),
    "H612D seg 17 (3rd mask byte)": (B.f_seg_rgb(255, 0, 0, 1 << 16, 3), "33 05 15 01 ff 00 00 00 00 00 00 00 00 00 01"),
    "H612D segs 1-20 5%":          (B.f_seg_brightness(5, 0xFFFFF, 3), "33 05 15 02 05 ff ff 0f 00"),
}
for name, (got, want) in FRAMES.items():
    check(f"frame: {name}", hexf(got).startswith(want), f"got  {hexf(got)}\n      want {want}…")

check("safety: kelvin clamps", B.f_kelvin(3, 9000)[5:7] == bytes([0x19, 0x64]) and B.f_seg_kelvin(1000, 1, 1)[7:9] == bytes([0x0a, 0x8c]))
try:
    B.f_rgb(3, 255, 0, 0)
    check("safety: lower light refuses RGB", False)
except ValueError:
    check("safety: lower light refuses RGB", True)


# ---------------------------------------------------------------- fakes
class FakeLamp:
    def __init__(self):
        self.sent, self.cmds, self.ip = [], [], "192.0.2.1"

    def command(self, cmd, data):
        self.cmds.append((cmd, data))

    def send(self, frames):
        self.sent.append([hexf(f) for f in frames])


class FakeMQTT:
    def __init__(self):
        self.pub = []

    def publish(self, t, p, retain=False):
        self.pub.append((t, p))


def dev_of(cls, **kw):
    d = cls("test", "Test", "192.0.2.1", "X", FakeMQTT(), **kw)
    d.lamp = FakeLamp()
    return d


def cmd(d, entity, payload):
    d.handle(entity, "set", payload if isinstance(payload, bytes) else json.dumps(payload).encode(), False, False)
    d.timer.cancel()
    d.flush()
    return d.lamp.sent[-1]


# ---------------------------------------------------------------- H60B0 (sections)
d = dev_of(B.SectionDevice)
cmd(d, "ripple", {"state": "ON", "color": {"r": 255, "g": 0, "b": 0}, "brightness": 128})
cmd(d, "side", {"state": "ON", "color_temp": 3000})
sent = cmd(d, "lower", {"state": "ON"})
check("H60B0 two-section limit: lower on switches ripple off in the same datagram",
      sent[0].startswith("33 30 01 00") and d.state["ripple"]["state"] == "OFF" and d.state["lower"]["state"] == "ON", sent)
n = len(d.lamp.sent)
cmd(d, "lower", {"color": {"r": 1, "g": 2, "b": 3}})
check("H60B0 colour never reaches the lower light", all(not f.startswith("33 05 2c 03 01") or len(f) == 59 and f[15:20] != "01 02" for f in d.lamp.sent[-1]))
sent = cmd(d, "side", {"state": "OFF"})
check("H60B0 section off is one frame", len(sent) == 1 and sent[0].startswith("33 30 02 00"), sent)
sent = cmd(d, "flow", b"75")
check("H60B0 flow rate", sent[0].startswith("33 05 2c 01 03 4b"), sent)

ok = True
for order in itertools.permutations([("ripple", {"state": "ON", "color": {"r": 9, "g": 9, "b": 9}}),
                                     ("side", {"state": "ON", "brightness": 100}), ("lower", {"state": "OFF"})]):
    d.state["ripple"]["state"], d.state["side"]["state"], d.state["lower"]["state"] = "OFF", "ON", "ON"
    d.on_order = ["side", "lower"]
    for name, c in order:
        d.handle(name, "set", json.dumps(c).encode(), False, False)
    d.timer.cancel()
    d.flush()
    got = {k: v["state"] for k, v in d.state.items()}
    ok &= got == {"ripple": "ON", "side": "ON", "lower": "OFF"} and not any(f.startswith("33 30 02 00") for f in d.lamp.sent[-1])
check("H60B0 scene race: every command order lands as defined", ok)

d = dev_of(B.SectionDevice)
d.state["side"]["state"], d.on_order = "ON", ["side"]
for i in range(1, 9):
    d.handle(f"side_segment_{i}", "set", json.dumps({"state": "ON", "brightness": 3 if i % 2 else 255}).encode(), False, False)
d.timer.cancel()
d.flush()
bri = sorted(f[:20] for f in d.lamp.sent[-1] if f.startswith("33 05 2c 02 02"))
check("H60B0 side segments alternate 1%/100% -> masks 0x55/0xaa", bri == ["33 05 2c 02 02 01 55", "33 05 2c 02 02 64 aa"], bri)
sent = cmd(d, "lamp", {"state": "OFF"})
check("H60B0 whole lamp off -> power off only", sent == [hexf(B.f_power(False))], sent)
on_pub = [p for t, p in d.mqtt.pub if t.endswith("/ripple/state")][-1]
check("H60B0 sections report off while the lamp is off", on_pub["state"] == "OFF")
sent = cmd(d, "lamp", {"state": "ON", "brightness": 128})
check("H60B0 whole lamp on + master brightness", sent[0].startswith("33 01 01") and any(f.startswith("33 04 32") for f in sent), sent)

# ---------------------------------------------------------------- segment family (H6088 / H61E0)
s = dev_of(B.SegmentDevice, segments=6)
check("H6088: 7 entities (lamp + 6 cubes)", s.entities() == ["lamp"] + [f"segment_{i}" for i in range(1, 7)])
s.lamp.cmds.clear()
sent = cmd(s, "lamp", {"state": "ON", "color": {"r": 0, "g": 255, "b": 0}, "brightness": 255})
check("H6088 whole lamp uses standard commands only (turn, brightness, colorwc; no ptReal)",
      [c for c, _ in s.lamp.cmds] == ["turn", "brightness", "colorwc"] and s.lamp.cmds[2][1]["color"] == {"r": 0, "g": 255, "b": 0}
      and sent == [], (s.lamp.cmds, sent))
check("H6088 whole colour updates every cube's state", all(c["color"] == {"r": 0, "g": 255, "b": 0} and c["on"] for c in s.segs))
sent = cmd(s, "segment_3", {"state": "OFF"})
check("H6088 cube off = black on that cube only", sent == [hexf(B.f_seg_rgb(0, 0, 0, 0x04, 1))], sent)
sent = cmd(s, "segment_3", {"state": "ON"})
check("H6088 cube back on restores its colour + brightness",
      any(f.startswith("33 05 15 01 00 ff 00 00 00 00 00 00 04") for f in sent) and any(f.startswith("33 05 15 02 64 04") for f in sent), sent)
for i in range(1, 7):
    s.handle(f"segment_{i}", "set", json.dumps({"state": "ON", "color": {"r": 255, "g": 0, "b": 0} if i % 2 else {"r": 0, "g": 0, "b": 255}}).encode(), False, False)
s.timer.cancel()
s.flush()
colour = [f for f in s.lamp.sent[-1] if f.startswith("33 05 15 01")]
check("H6088 6 cubes in 2 colours -> 2 colour frames (masks 0x15 / 0x2a)", len(colour) == 2 and len(s.lamp.sent[-1]) <= 12, s.lamp.sent[-1])
for i in range(1, 7):
    s.handle(f"segment_{i}", "set", b'{"state": "OFF"}', False, False)
s.timer.cancel()
s.flush()
check("H6088 last cube off -> lamp off (standard turn)", s.whole["state"] == "OFF" and s.lamp.cmds[-1] == ("turn", {"value": 0}), s.lamp.cmds[-1:])
s.lamp.cmds.clear()
sent = cmd(s, "segment_2", {"state": "ON", "color_temp": 3000})
check("H6088 cube on while lamp off -> standard power on, then the ptReal pattern",
      [c for c, _ in s.lamp.cmds] == ["turn", "brightness"] and s.whole["state"] == "ON" and any(f.startswith("33 05 15") for f in sent), (s.lamp.cmds, sent))
s.lamp.cmds.clear()
cmd(s, "lamp", {"color_temp": 9000})
check("H6088 whole-lamp temperature via colorwc, clamped", s.lamp.cmds == [("colorwc", {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": 6500})], s.lamp.cmds)
s.lamp.cmds.clear()
cmd(s, "lamp", {"state": "OFF"})
check("H6088 whole lamp off = one standard turn command", s.lamp.cmds == [("turn", {"value": 0})], s.lamp.cmds)

w = dev_of(B.SegmentDevice, segments=15)
check("H61E0: 16 entities, 2-byte mask", len(w.entities()) == 16 and w.width == 2 and w.all == 0x7FFF)
sent = cmd(w, "segment_12", {"state": "ON", "color": {"r": 255, "g": 0, "b": 255}})
check("H61E0 segment 12 uses the second mask byte", any(f.startswith("33 05 15 01 ff 00 ff 00 00 00 00 00 00 08") for f in sent), sent)
w.lamp.cmds.clear()
check("H61E0 whole-strip colour is one standard colorwc, no ptReal", cmd(w, "lamp", {"color": {"r": 1, "g": 2, "b": 3}}) == [] and w.lamp.cmds[-1][0] == "colorwc")

x = dev_of(B.SegmentDevice, segments=B.PROFILES["H612D"]["segments"])
check("H612D: 21 entities, 3-byte mask", len(x.entities()) == 21 and x.width == 3 and x.all == 0xFFFFF)
sent = cmd(x, "segment_20", {"state": "ON", "color": {"r": 0, "g": 255, "b": 0}})
check("H612D segment 20 uses the third mask byte", any(f.startswith("33 05 15 01 00 ff 00 00 00 00 00 00 00 00 08") for f in sent), sent)

# ---------------------------------------------------------------- inventory / devices.toml
tmp = tempfile.mkdtemp()
cfg = os.path.join(tmp, "devices.toml")
inv = B.Inventory(path=cfg, url="http://unused")
API = [{"sku": "H60B0", "id": "AA:01", "name": "H60B0_AB12", "ip": "192.0.2.10", "room": "Living Room"},
       {"sku": "H6088", "id": "AA:02", "name": "H6088_CD34", "ip": "192.0.2.11"},
       {"sku": "H61E0", "id": "AA:03", "name": "H61E0_EF56", "ip": "192.0.2.12"},
       {"sku": "H6004", "id": "AA:04", "name": "H6004_0B96", "ip": "192.0.2.13"}]
entries = inv.sync(API)
text = open(cfg, encoding="utf8").read()
check("config written with every govee2mqtt light", all(f'id = "{d["id"]}"' in text for d in API))
by = {e["id"]: e for e in entries}
check("key = room + name when the light has a Govee room", by["AA:01"]["key"] == "living_room_h60b0_ab12", by["AA:01"])
check("key = device name when no room", by["AA:03"]["key"] == "h61e0_ef56", by["AA:03"])
check("config file created from scratch (no manual setup)", text.startswith("# govee-lan-bridge devices"))
check("supported models enabled, unsupported recorded but disabled",
      by["AA:02"]["enabled"] and by["AA:03"]["enabled"] and not by["AA:04"]["enabled"] and "no built-in profile" in text)
with open(cfg, "a", encoding="utf8") as f:
    pass
text = text.replace('key = "h6088_cd34"', 'key = "hallway_sconces"').replace('name = "H6088_CD34"', 'name = "Hallway Sconces"')
text = text.replace('# my note', '') + "\n# my own comment\n"
open(cfg, "w", encoding="utf8").write(text)
API[1]["ip"] = "192.0.2.99"
entries = inv.sync(API)
text2 = open(cfg, encoding="utf8").read()
check("user edits and comments survive a resync", 'key = "hallway_sconces"' in text2 and "# my own comment" in text2)
check("IP change updated in place", 'ip = "192.0.2.99"' in text2 and text2.count('id = "AA:02"') == 1)
check("no duplicate entries on resync", text2.count("[[device]]") == 4)
entries = inv.sync([])                     # govee2mqtt unavailable -> run from the file
devs = [B.make_device(e, FakeMQTT()) for e in entries]
devs = [x for x in devs if x]
check("from the file alone: 3 bridged lights with the right families",
      sorted((x.key, type(x).__name__) for x in devs) == [("h61e0_ef56", "SegmentDevice"), ("hallway_sconces", "SegmentDevice"),
                                                         ("living_room_h60b0_ab12", "SectionDevice")], [(x.key, type(x).__name__) for x in devs])
check("Govee room becomes the suggested HA area",
      [x for x in devs if x.key == "living_room_h60b0_ab12"][0].device_info().get("suggested_area") == "Living Room")
dup = B.Inventory(path=os.path.join(tmp, "dup.toml"), url="x").sync(
    [{"sku": "H6088", "id": "D1", "name": "Sconces", "ip": "192.0.2.50"}, {"sku": "H6088", "id": "D2", "name": "Sconces", "ip": "192.0.2.51"}])
check("two identical lights get distinct keys", sorted(e["key"] for e in dup) == ["sconces", "sconces_2"], [e["key"] for e in dup])
text3 = text2.replace('sku = "H6004"', 'sku = "H6004"\nfamily = "segments"\nsegments = 4').replace(
    'name = "H6004_0B96"\nenabled = false', 'name = "H6004_0B96"\nenabled = true')
open(cfg, "w", encoding="utf8").write(text3)
user = [B.make_device(e, FakeMQTT()) for e in inv.sync([])]
check("a user-described model gets bridged", any(x and x.key == "h6004_0b96" and x.n == 4 for x in user))

# Entity naming scheme for the H60B0 (unique IDs and discovery topics derive from the key).
up = B.SectionDevice("living_room_uplight", "Living Room Uplight", "192.0.2.10", "H60B0", FakeMQTT())
up.discover()
uids = {p["unique_id"] for t, p in up.mqtt.pub if t.endswith("/config")}
check("H60B0 unique IDs follow <key>_<entity>", {"living_room_uplight_ripple", "living_room_uplight_side", "living_room_uplight_lower",
                                  "living_room_uplight_flow_rate", "living_room_uplight_side_segment_1"} <= uids)
check("H60B0 discovery topics", any(t == "homeassistant/light/living_room_uplight/ripple/config" for t, _ in up.mqtt.pub))

print("\nALL TESTS PASSED" if not fails else f"\n{fails} FAILED")
raise SystemExit(1 if fails else 0)
