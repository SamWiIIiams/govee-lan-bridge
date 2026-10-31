# govee-lan-bridge

[![Docker Hub](https://img.shields.io/docker/v/swilli23/govee-lan-bridge?sort=semver&label=docker%20hub&logo=docker)](https://hub.docker.com/r/swilli23/govee-lan-bridge)
[![Image size](https://img.shields.io/docker/image-size/swilli23/govee-lan-bridge/2?logo=docker)](https://hub.docker.com/r/swilli23/govee-lan-bridge)
[![Tests and image](https://github.com/SamWiIIiams/govee-lan-bridge/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/SamWiIIiams/govee-lan-bridge/actions/workflows/docker-publish.yml)

Per-section and per-segment control of Govee lights from Home Assistant, **LAN only**: no Govee cloud,
no API key, no internet connection.

Govee's documented LAN API only controls a light as a whole. This bridge sends the lights' own segment
and section commands over LAN (`ptReal`) and exposes them to Home Assistant through MQTT discovery. It
finds your lights through [govee2mqtt](https://github.com/wez/govee2mqtt) and keeps a config file of them,
so new lights show up on their own, and the bridge can keep running from that file without govee2mqtt.

## Supported lights
| Model | Profile | Entities |
|---|---|---|
| **H60B0** Uplighter Floor Lamp | `sections` | whole lamp; Ripple, Side, Lower sections; 8 Side segments; flow rate |
| **H6088** RGBIC Cube Wall Sconces | `segments` (6) | whole lamp; one light per cube |
| **H61E0** LED Strip Light M1 | `segments` (15) | whole lamp; one light per segment |
| **H612D** 49.2 ft RGBICW LED Strip Light | `segments` (20) | whole lamp; one light per segment |

Other lights using the same command families can be added in the config file without code changes
(see [Adding a light](#adding-a-light)).

Each light gets a whole-lamp light (`light.<key>_lamp`), so you can disable govee2mqtt's entity for it
and use the bridge alone. The key comes from govee2mqtt: the Govee app room plus the device name if a room
is set (e.g. `living_room_h6088_1a2b`), otherwise the device name (`h6088_1a2b`). The room also becomes the
device's suggested area in Home Assistant. Rename entities in Home Assistant as you like.

### Segment numbering
On the H61E0 strip, segment 1 is the end nearest the power/data input and the numbers
increase away from it. Segments 7–10 have blend zones between them, so they cover a longer run of the strip
and colours there fade into each other; 1–6 and 11–15 are equal-length, directly adjacent blocks. On the H6088
it's the opposite: cube 1 is the one farthest from the power input. The H612D is two runs from one controller:
segments 1–10 are one run and 11–20 the other, each numbered from the controller outward.

### H60B0 entities
| Entity | Controls |
|---|---|
| `light.<key>_lamp` | power, master brightness |
| `light.<key>_ripple` | top: on/off, brightness, RGB, 2700–6500 K |
| `light.<key>_side` | middle ring (all 8 segments): on/off, brightness, RGB, 2700–6500 K |
| `light.<key>_lower` | down light: on/off, brightness, 2700–6500 K |
| `light.<key>_side_segment_1` … `_8` | each side segment |
| `number.<key>_flow_rate` | rotation speed of the ripple lens, 0–100 (0 ≈ stopped) |

## Requirements
- Lights with **LAN Control** enabled in the Govee Home app
- An MQTT broker with Home Assistant's MQTT integration (discovery prefix `homeassistant`)
- [govee2mqtt](https://github.com/wez/govee2mqtt) on the same LAN for automatic discovery (optional: lights
  can also be listed by hand in `devices.toml`)
- Docker with host networking on the same LAN as the lights

## Run
Prebuilt images (amd64 and arm64) are on Docker Hub as
[`swilli23/govee-lan-bridge`](https://hub.docker.com/r/swilli23/govee-lan-bridge); no build needed.
Use the `2` tag to get v2 updates without future breaking changes.

```yaml
services:
  govee-lan-bridge:
    image: swilli23/govee-lan-bridge:2
    restart: unless-stopped
    network_mode: host
    volumes:
      - /path/to/govee-lan-bridge:/config   # created automatically; devices.toml is generated here
    environment:
      MQTT_HOST: 127.0.0.1
      GOVEE2MQTT_URL: http://127.0.0.1:8056
```

| Variable | Default | |
|---|---|---|
| `MQTT_HOST` / `MQTT_PORT` | `127.0.0.1` / `1883` | Broker |
| `MQTT_USER` / `MQTT_PASS` | *(none)* | Broker credentials, if required |
| `MQTT_PASS_FILE` | *(none)* | File holding the password instead of `MQTT_PASS` (e.g. `/run/secrets/mqtt_pass`) |
| `GOVEE2MQTT_URL` | `http://127.0.0.1:8056` | govee2mqtt web UI (device list at `/api/devices`) |
| `CONFIG_PATH` | `/config/devices.toml` | Config file |
| `DISCOVERY_INTERVAL` | `600` | Seconds between govee2mqtt refreshes |
| `DISCOVERY_PREFIX` | `homeassistant` | MQTT discovery prefix |
| `BATCH_WINDOW` | `0.15` | Seconds within which commands are merged into one change |
| `MAX_FRAMES_PER_PACKET` | `12` | Commands per LAN packet before splitting |

## devices.toml
Nothing to set up: the bridge creates this file on first start and maintains it. Every light govee2mqtt
reports is added, and IP addresses are kept current. Your edits and comments are preserved. (The bridge
starts as root only to make the config folder writable, then runs as an unprivileged user.)

```toml
[[device]]
id = "AA:BB:CC:DD:EE:FF:00:11"   # Govee device ID (stable; identifies the light)
sku = "H6088"
ip = "192.0.2.182"               # updated automatically
key = "bedroom_sconces"          # entity ID prefix
name = "Bedroom Sconces"         # device name in Home Assistant
enabled = true
```

- Changing a light's `key` creates new entities. To get friendlier entity IDs, it's usually easier to
  rename the entities in Home Assistant.
- Lights without a profile are recorded with `enabled = false`.
- Several lights of the same model are independent: each has its own entities, state and command queue.
- If govee2mqtt is unreachable, the bridge runs from this file.

### Adding a light
For a light the bridge doesn't know yet, add `family` (and `segments`) to its entry and enable it:
```toml
family = "segments"   # lights using the 33 05 15 command family
segments = 10
enabled = true
```

## Behaviour
- **Scenes apply atomically.** Commands for a light arriving within 150 ms are merged into one change
  and sent in one packet, whatever order Home Assistant sends them in.
- **Packet size.** The lights accepted 18 `ptReal` commands per packet in testing but dropped 24 **entirely**;
  the bridge sends at most 12 per packet.
- **Pacing.** Each command keeps a light busy for a moment, and a packet arriving while it is busy can be
  silently dropped, whether it's a `ptReal` packet or a standard command right after one. In testing, 50 ms
  spacing lost packets and 100 ms still lost one in about nine mixed sequences; 150 ms lost none. So the bridge
  sends every command to a light in order and at least 150 ms apart, and bundles frames into as few packets
  as possible.
- **Segments (`segments` profile).** Whole-lamp power, brightness and colour use Govee's standard LAN
  commands (documented, reflected in the light's own status, and never dropped when sent back-to-back);
  `ptReal` is used only for per-segment changes, sent after any standard commands in the same change.
  The whole-lamp light repaints every segment; segment lights control one each. A segment's "off" is **black**, which is a true off. Turning the last segment off switches the
  lamp off; turning one on while the lamp is off powers it on with the saved pattern.
- **H60B0.** Two sections can be on at once: turning on a third switches off the one that's been on
  longest. Side segments are diffuse, so a side segment's "off" is minimum brightness. Turning the last lit
  segment off switches the Side section off.
- **Safety:** colour temperatures are clamped to 2700–6500 K, and the H60B0's down light never receives
  colour bytes. Either mistake can black out the lamp until a valid setting is sent.
- **State is "last set".** The lights can't report segment or section state over LAN, so changes made in
  the Govee app or by govee2mqtt aren't reflected. State is retained in MQTT across restarts.

## Upgrading from v1
v1 configured one H60B0 with `LAMP_IP` / `DEVICE_ID` / `DEVICE_NAME`; v2 ignores those and discovers lights
instead, so the lamp's entities get a new key. Remove the old (now unavailable) v1 entities in Home
Assistant, then rename the new ones to the old entity IDs if scenes or dashboards refer to them.

## Protocol notes
Commands are standard 20-byte Govee frames (data, zero padding, XOR checksum), sent base64-encoded in a
LAN `ptReal` message to UDP port 4003. Several frames can go in one message's `command` array.

**All lights** (the bridge uses these for the H60B0; segment lights use the standard `turn` / `brightness` / `colorwc` commands instead)
| Function | Frame data |
|---|---|
| Power on/off | `33 01 01` / `33 01 00` |
| Master brightness | `33 04 <1-100>` |

**`segments` family (H6088, H61E0, H612D)**: the mask is 1 byte per 8 segments (1–3 bytes), little-endian; bit n = segment n+1
| Function | Frame data |
|---|---|
| Segment colour | `33 05 15 01 RR GG BB 00 00 00 00 00 <mask>` |
| Segment temperature | `33 05 15 01 00 00 00 KK KK 00 00 00 <mask>` (Kelvin big-endian) |
| Segment brightness | `33 05 15 02 <1-100> <mask>` (mask right after the value) |
| Segment off | colour `00 00 00` (brightness 0 only dims) |

**H60B0**
| Function | Frame data |
|---|---|
| Section on/off (1 Ripple, 2 Side, 3 Lower) | `33 30 <sec> 01` / `33 30 <sec> 00` |
| Ripple colour / temperature | `33 05 2c 01 01 RR GG BB KK KK` (`00 00` for plain colour) |
| Ripple brightness | `33 05 2c 01 02 <1-100>` |
| Ripple flow rate | `33 05 2c 01 03 <0-100>` |
| Side colour / temperature | `33 05 2c 02 01 RR GG BB KK KK <segment mask>` |
| Side brightness | `33 05 2c 02 02 <1-100> <segment mask>` (`ff` = all) |
| Lower temperature | `33 05 2c 03 01 KK KK` (**no** RGB bytes) |
| Lower brightness | `33 05 2c 03 02 <1-100>` |

H60B0 commands were derived from the Govee Home app's Bluetooth traffic; all commands were verified on real
hardware over LAN.

## Tests
```
python test_bridge.py      # Python 3.11+
```
Checks every frame byte-for-byte against captured or live-verified commands, plus each profile's rules,
the scene batching and the config file handling. It runs offline, and the GitHub Action
runs it before publishing an image.

## Contributing
Have a Govee light that isn't supported yet? [Request support for it](../../issues/new?template=new_device.yml):
the form asks for the details needed to add it (model, what the Govee app can control, and whether you can run
a few short visual tests). Many lights share a command family, so a new model is often just a profile.
Pull requests with a new profile and its tests are welcome too.

## Disclaimer
Unofficial and not affiliated with or endorsed by Govee. It uses undocumented LAN commands, which a
firmware update could change. Invalid settings can leave a light dark until a valid setting is sent (see
Behaviour); no permanent effects were seen in testing. Use at your own risk.

## License
MIT. See [LICENSE](LICENSE).
