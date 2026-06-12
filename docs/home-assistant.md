# Home Assistant: synthetic motion sensors from detection zones

HomeTwin's presence sensing (RF tomography, door/drawer activity — any
presence-class signal) can drive Home Assistant exactly like hardware
PIRs: you draw **detection zones** on the map, and each zone registers
as its own HA device with a motion `binary_sensor` entity via MQTT
discovery. Automations ("lights on when living-room motion") need no
HomeTwin knowledge at all.

## Configure

```yaml
home_assistant:
  mqtt:
    host: 192.168.1.5        # your HA / Mosquitto broker
    port: 1883
    username: hometwin       # optional
    password: "..."          # optional
  off_delay_s: 30            # default motion-clear delay

motion_zones:                # each entry = one HA device
  - name: living-room-motion
    min: [0, 0, 0]
    max: [5, 4, 2.6]
  - name: hallway-motion
    center: [6.0, 2.0]       # circles: z ignored
    radius: 1.2
    off_delay_s: 60
```

Zones appear in HA automatically (MQTT discovery, retained config) as
devices named "HomeTwin living-room-motion" etc., with availability
tracking. States publish on transition only — no chatter.

## Drawing zones on the map

**Shift-drag** on the dashboard map draws a new zone (you'll be asked
for a name); it's created live via `POST /motion-zones` and announced
to HA immediately. Runtime zones last until restart — copy the config
snippet to keep them. The "Motion zones" layer shows every zone
state-colored (red = motion).

## Semantics

- ON immediately when presence evidence lands inside the zone (blob
  centroid within `sigma * 0.5` of the region — a fuzzy detection near
  the edge still counts).
- OFF after `off_delay_s` without evidence, like PIR integrations.
- Evidence sources today: tomography presence blobs and door/drawer
  movement (a drawer opening IS motion at its location). New
  presence-class sensors feed the same path automatically.

## Implementation notes

The MQTT client is a minimal publish-only stdlib implementation
(QoS 0, retained discovery/state, availability online/offline) — no new
dependencies, and a broker outage is absorbed with backoff without ever
touching tracking. The wire protocol is tested against a real socket
broker stub in `tests/test_hass.py`.
