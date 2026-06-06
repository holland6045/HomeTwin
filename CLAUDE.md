# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

RGBDesk is a smart desk system built on multiple embedded microcontrollers. It integrates touch displays, addressable LEDs, and various sensors. The project targets four MCU families managed under a single PlatformIO workspace:

| Target | Role |
|---|---|
| **ESP32** | Wi-Fi/BT hub, web server, OTA, primary coordinator |
| **RP2040** | USB HID, fast PWM, high-frequency sensor polling |
| **STM32** | Motor control, precision timing, peripheral I/O |
| **AVR/Arduino** | Simple peripheral nodes, legacy compatibility |

## Build System: PlatformIO

All targets are defined as named environments in `platformio.ini`. Every command below accepts `-e <env>` to scope to a single target.

```bash
# Build all environments
pio run

# Build a specific target
pio run -e esp32

# Flash firmware (connect device first)
pio run -e esp32 -t upload

# Open serial monitor (baud rate set per environment in platformio.ini)
pio device monitor -e esp32

# Build + flash + open monitor in one step
pio run -e esp32 -t upload && pio device monitor -e esp32

# Run unit tests (native or on-device depending on environment)
pio test -e native
pio test -e esp32

# Run a single test file
pio test -e native -f test_led_controller

# Static analysis
pio check -e esp32

# List detected connected devices
pio device list

# Clean build artifacts
pio run -t clean
```

## Repository Structure

```
platformio.ini          # All environment definitions, board configs, build flags
src/                    # Per-target main entry points (main.cpp per env or subdir)
include/                # Shared headers across all targets
lib/                    # Local libraries — shared business logic, HAL interfaces
  ├── RGBController/    # LED abstraction layer
  ├── SensorBus/        # Sensor polling and data aggregation
  ├── DisplayDriver/    # Touch display abstraction
  └── DeskProtocol/     # Inter-MCU communication protocol (UART/I2C/SPI)
test/                   # Unity-based unit tests
  ├── native/           # Host-runnable tests (no hardware needed)
  └── embedded/         # On-device tests
data/                   # SPIFFS/LittleFS assets for ESP32 (web UI, config files)
```

## Architecture Principles

### Modularity & Binary Footprint
Every hardware integration is opt-in. A target that doesn't use a driver must pay zero ROM/RAM cost for it — no global constructors, no linked symbols, no pulled-in library headers.

Rules:
- Concrete driver `.cpp` files wrap their entire body in the relevant build flag (`#ifdef LED_DRIVER_WS2812B`, `#ifdef DISPLAY_SPI_TFT`, etc.). If the flag is absent the translation unit is empty.
- Hardware library headers (`FastLED.h`, `TFT_eSPI.h`, sensor SDK headers) are included only inside concrete driver `.cpp` files — never in `include/` or `lib/` shared headers.
- `platformio.ini` uses per-environment `lib_deps` and `lib_ignore` to prevent unused libraries from linking at all. Do not rely solely on linker `--gc-sections` to strip dead code.
- Adding a new sensor, display, or LED driver must not increase binary size on any environment that does not set its flag.
- The `native` test environment is the proof: it compiles all shared `lib/` code with no hardware flags set and no hardware libraries installed. If `pio test -e native` breaks when a new driver is added, the driver violated the isolation rule.

### Network Topology
The system supports a two-tier hierarchy: one primary coordinator (ESP32) and any number of **hub nodes** that each manage a local cluster of leaf nodes. Direct leaf-to-coordinator connections are allowed but not required.

```
ESP32 coordinator
├── Hub A  (e.g. RP2040 — left desk zone)
│   ├── Leaf: LED driver
│   ├── Leaf: sensor array
│   └── Leaf: AVR peripheral node
├── Hub B  (e.g. ESP32 — right desk zone, wireless via ESP-NOW)
│   ├── Leaf: display node
│   └── Leaf: touch controller
└── Direct leaf: STM32 motor controller
```

**Hub responsibilities:**
- Runs the same heartbeat and discovery logic as the coordinator for its own leaf nodes.
- Maintains its own per-leaf state ring buffer; leaf catch-up is handled locally without coordinator involvement.
- Aggregates and forwards leaf data upstream using the standard `DeskProtocol` packet format — the coordinator sees hub-sourced data as normal node readings with a `hub_id` routing field.
- May execute local control loops (e.g. LED zone animation, sensor threshold alerts) without round-tripping to the coordinator, reducing latency and bus load.

**Coordinator rules:**
- Treats a hub as a single addressable node; does not need to know the topology beneath it.
- If a hub disconnects, all of its leaves are implicitly marked offline — the coordinator does not attempt to contact them directly.
- A hub may be promoted to direct-connect or demoted back to hub role via a runtime config packet; no firmware reflash required.

**Transport flexibility:**
- Hub ↔ coordinator link: UART, I2C, SPI, or ESP-NOW (wireless hubs).
- Hub ↔ leaf link: any supported bus; the hub owns all bus-master responsibilities for its cluster.
- Routing field in `DeskProtocol` packets: `coordinator_id / hub_id / node_id` — three bytes, zero for unused tiers.

### Node Resilience
The system must degrade gracefully when any MCU node disconnects and resume automatically when it reconnects or a new node joins — no coordinator firmware change required.

**Liveness:**
- Every node sends a periodic heartbeat packet over `DeskProtocol`. The ESP32 coordinator marks a node offline after a configurable missed-heartbeat threshold (default: 3 consecutive missed beats).
- Offline nodes do not block any coordinator loop, sensor poll, or LED update. The coordinator continues operating on the last valid state for that node.
- When a node reconnects it re-announces itself; the coordinator re-registers it without restart.

**Discovery:**
- Nodes announce their capabilities (sensor types, LED zones, display role) in a registration packet on boot. The coordinator builds its device map at runtime — never hardcode which nodes exist or what they provide.
- A new node type must be addable without modifying firmware on any existing node.

**State history & catch-up:**
- The coordinator maintains a fixed-depth ring buffer of timestamped state snapshots per node (size configurable via build flag, default 32 entries). Entries cover sensor `Reading` values, LED zone states, and any config deltas.
- On reconnect, the node sends its last-known timestamp in the registration packet. The coordinator replays buffered entries newer than that timestamp in chronological order before resuming live data.
- Buffer depth is bounded — overflow discards oldest entries. Nodes that reconnect after a gap larger than the buffer window receive only the most recent snapshot and a `STALE` flag; they must not assume continuity.
- Buffers are allocated statically per node slot at startup. No heap growth on reconnect.

**Safe defaults:**
- On node loss, actuators (LEDs, display) hold their last committed state. Sensors drop to `Reading::invalid`; consumers that check the validity flag degrade silently.
- No subsystem may assert or halt because a peer node is absent. Fail-safe, not fail-stop.

### Hardware Abstraction Layer (HAL)
All hardware-specific code lives behind interfaces defined in `include/`. Concrete implementations go in `lib/`. Tests use mock implementations. Never call platform-specific APIs (e.g., `digitalWrite`, `gpio_set_level`) directly in business logic — wrap them.

### Inter-MCU Protocol
MCUs communicate over UART or I2C using a shared packet format defined in `lib/DeskProtocol/`. The ESP32 acts as the bus master. New message types require updating the protocol definition and all listening nodes.

**Three logical channels** (inspired by PROFINET's CR model — never conflate them, they have different priorities and delivery guarantees):

| Channel | `msg_class` | Delivery | Use |
|---|---|---|---|
| Cyclic | `0` | Push, no ack | Sensor readings, LED/actuator commands at fixed interval |
| Acyclic | `1` | Confirmed request/response | Config writes, parameter reads, firmware version queries |
| Alarm | `2` | Confirmed, preempts acyclic | Fault notifications, node plug/pull events |

A long acyclic transfer must never block an incoming alarm. On UART, the ISR checks `msg_class` on each frame header and inserts alarm frames ahead of queued acyclic traffic.

**Cyclic frame structure:**
```cpp
struct CyclicFrame {
    uint8_t  node_id;
    uint8_t  msg_class;   // = 0
    uint16_t frame_id;    // identifies the data source (slot/subslot)
    uint8_t  iops;        // provider status: 0x80 = GOOD, 0x00 = BAD
    uint8_t  payload[CYCLIC_PAYLOAD_MAX];
    uint8_t  iocs;        // consumer status echoed back in opposite direction
};
```
Consumers must check `iops == 0x80` before using payload data. If `iops == 0x00`, hold last known good value and raise an alarm.

**Data consistency — double buffer:**
Each node maintains shadow and active frame buffers. Sensor DMA/ISR writes to the shadow buffer and sets `swap_pending`. The cyclic send tick atomically swaps shadow→active before transmitting. The active buffer is never written during transmission — no mutex needed, no torn reads.

**Node capability model:**
At boot each node sends a registration packet describing its slots: `{slot, subslot, direction, data_type, size_bytes}`. The ESP32 validates this against its stored `data/expected_config.json`. Mismatches raise a `PLUG` alarm; the node is accepted into `DEGRADED` mode rather than rejected outright. Node descriptors are stored as `data/<node_type>.json` (SPIFFS) and exposed via the ESP32 web API — allowing a companion tool to introspect the live system without firmware knowledge.

**Commissioning state machine (every node):**
```
OFFLINE → WAITING_CONNECT → PARAMETERIZING → OPERATE
```
- Node rejects cyclic output commands until it reaches `OPERATE` — prevents actuator commands reaching hardware before parameterization completes.
- On coordinator loss: `OPERATE → WAITING_CONNECT` (Layer 2 watchdog). Actuators enter safe state immediately (Layer 1 watchdog).
- Re-entry to `OPERATE` after reconnect does not require a full re-parameterize if the coordinator sends a `RESUME` flag and the node's config hash matches.

**Alarm model:**
```cpp
enum AlarmType : uint8_t { ALARM_DIAGNOSIS=0, ALARM_PROCESS=1, ALARM_PULL=2, ALARM_PLUG=3 };
enum AlarmSeverity : uint8_t { SEV_FAULT=0, SEV_MAINTENANCE=1, SEV_WARNING=2 };

struct AlarmFrame {
    uint8_t  node_id, hub_id;
    uint8_t  alarm_type;      // AlarmType
    uint8_t  severity;        // AlarmSeverity
    uint8_t  slot, subslot;
    uint16_t error_code;      // standardized: 0x0001 short-circuit, 0x0002 wire-break, 0x0004 underrange, 0x0008 overrange
    uint8_t  disappears;      // 1 = fault cleared
    uint32_t timestamp_ms;
};
```
Alarm delivery is confirmed: the node holds the next alarm until the coordinator sends `ALARM_ACK`. Maximum 4 pending alarms per AVR node. The ESP32 maintains an active alarm registry (`AlarmEntry[]`); entries are added on receipt and removed on `disappears=1`. The web dashboard reads this registry — not individual node poll results.

**Two-layer watchdog (every node):**
- **Layer 1 — cycle watchdog:** If no valid cyclic frame arrives within `cycle_ms × DataHoldFactor` (default factor=3), outputs transition to safe state and `iops` is set to `0x00`. The node stays connected.
- **Layer 2 — AR watchdog:** If no frames arrive for `AR_TIMEOUT_MS` (default 5000 ms), the node drops to `WAITING_CONNECT` and releases all resources.
- On valid frame receipt: reset both timers, restore `iops=0x80`, exit safe state.

Per-node safe states: LED driver → all pixels black + `show()`; motor → zero torque + brake; GPIO nodes → all outputs to defined inactive level.

### Environment-Specific Build Flags
Use `build_flags` in `platformio.ini` to compile platform-specific code paths rather than `#ifdef ARDUINO` / `#ifdef ESP_PLATFORM` scattered through source files. Keep preprocessor guards confined to HAL implementations.

### LED Pipeline
The LED state is computed centrally (on ESP32 or the coordinating MCU) and pushed to LED drivers. Animations run as state machines, not blocking delays. Never call `delay()` in LED or sensor code.

`lib/RGBController/` abstracts over three supported protocols behind a common `ILEDDriver` interface. Select the active driver at compile time via a build flag in `platformio.ini`:

| Protocol | Flag | Library | Notes |
|---|---|---|---|
| WS2812B / NeoPixel | `-D LED_DRIVER=WS2812B` | FastLED | Single-wire, 3-channel RGB |
| SK6812 / RGBW | `-D LED_DRIVER=SK6812` | FastLED | Single-wire, 4-channel RGBW |
| PWM RGB strip | `-D LED_DRIVER=PWM_RGB` | platform ledc / analogWrite | Non-addressable, 3 PWM channels |

Rules for all driver implementations:
- Never write to LED hardware outside of `ILEDDriver::show()` — callers only touch the pixel buffer.
- PWM_RGB maps the buffer's first pixel to the three PWM channels; treat it as a single-zone driver.
- RGBW content: pass a white component explicitly; do not auto-derive white from RGB values.
- FastLED's `addLeds<>` call and the PWM channel setup both live in the concrete driver constructor — nowhere else.

### Display Pipeline

`lib/DisplayDriver/` abstracts over five supported display interfaces and three touch input methods behind a common `IDisplayDriver` / `ITouchDriver` pair. Select at compile time via build flags in `platformio.ini`.

**Display interfaces**

| Interface | Flag | Typical controllers | Notes |
|---|---|---|---|
| SPI TFT | `-D DISPLAY=SPI_TFT` | ILI9341, ST7789, ST7735 | Most common small/mid panels |
| I2C OLED | `-D DISPLAY=I2C_OLED` | SSD1306, SSD1327 | Monochrome/grayscale, slow refresh |
| Parallel 8/16-bit | `-D DISPLAY=PARALLEL` | RA8875, NT35510 | Higher bandwidth for large panels |
| RGB parallel | `-D DISPLAY=RGB_PARALLEL` | ESP32-S3 native RGB panel | MIPI-style, requires dedicated ESP32-S3 |
| LED matrix | `-D DISPLAY=LED_MATRIX` | MAX7219, HUB75 | Treat as low-res pixel buffer |

**Touch controllers**

| Method | Flag | IC | Notes |
|---|---|---|---|
| I2C capacitive | `-D TOUCH=I2C_CAP` | FT5x06, GT911 | Interrupt-driven; poll only on IRQ pin |
| SPI resistive | `-D TOUCH=SPI_RES` | XPT2046 | Shares SPI bus with display; calibrate per-unit |
| None | `-D TOUCH=NONE` | — | Display-only nodes |

Rules for all driver implementations:
- All drawing calls go through `IDisplayDriver` — never call controller registers directly from application code.
- `ITouchDriver::read()` returns normalized coordinates (0–1 float); mapping to pixel space happens in the display layer, not callers.
- The UI framework (TBD) is injected on top of `IDisplayDriver`; keep `IDisplayDriver` framework-agnostic.
- RGB parallel and LED matrix drivers must not be linked on RP2040 or AVR targets — guard with platform checks in `platformio.ini`.
- Each display node declares exactly one `DISPLAY` flag and at most one `TOUCH` flag; no runtime switching.

### Sensor Framework

`lib/SensorBus/` owns all sensor integration. Every sensor implements `ISensor` and is registered with `SensorBus`, which drives polling, owns the reading schedule, and forwards data to the ESP32 coordinator via `DeskProtocol`.

**Supported sensor categories**

| Category | Example ICs | Bus |
|---|---|---|
| Air quality / CO2 | SCD40, SGP30, CCS811, MH-Z19B | I2C / UART |
| Temperature / humidity | BME280, SHT31, DHT22, DS18B20 | I2C / 1-Wire |
| Presence / proximity | LD2410 (mmWave), HC-SR04 (ultrasonic), PIR | UART / GPIO |
| Ambient light | BH1750, VEML7700, TSL2591 | I2C |
| Power / current | INA219, INA226 | I2C |
| IMU / accelerometer | MPU6050, LSM6DS3 | I2C / SPI |
| Capacitive touch | MPR121, TTP223 | I2C / GPIO |
| Microphone / sound | INMP441, MAX4466 | I2S / ADC |

**Bus protocol rules**

| Bus | Rule |
|---|---|
| I2C | All devices on a shared bus must have unique addresses; document address jumpers in hardware notes. Use interrupt pin where available — do not busy-poll. |
| SPI | One CS pin per sensor; never share CS lines. |
| UART | Each UART sensor gets its own `UARTSensor` wrapper that owns the serial port and parses the device-specific framing. |
| 1-Wire | DS18B20 chains attach to a single GPIO; the driver resolves ROM codes at init and maps them to named sensor slots. |
| ADC | Always oversample (minimum 16×) and apply a moving average before publishing. Raw ADC values must never leave the driver. |
| I2S | I2S microphone data is processed in DMA callbacks only; do not copy I2S buffers to heap in an ISR. |

**`ISensor` interface contract**

```cpp
class ISensor {
public:
    virtual bool     init()   = 0;   // called once at boot; return false to mark offline
    virtual void     update() = 0;   // called by SensorBus on its tick; must be non-blocking
    virtual Reading  read()   = 0;   // returns latest cached value; never triggers I/O
};
```

- `update()` must return in < 1 ms; defer slow operations (UART reads, I2S processing) to a background task or state machine.
- `SensorBus` assigns each sensor a poll interval via `registerSensor(sensor, intervalMs)`. Sensors with different rates (e.g. CO2 every 5 s, IMU every 10 ms) are handled by the scheduler — do not implement your own timing inside `update()`.
- Offline sensors (init returns false) are retried on a 30 s back-off; they must not block the bus loop.
- All `Reading` values carry a timestamp (`uint32_t ms`) and a validity flag; consumers must check validity before use.

### Shared Libraries
Code in `lib/` must compile cleanly on all target platforms unless guarded by a platform check in `platformio.ini`. Prefer pure C++ with no platform assumptions in library headers.

## PlatformIO Environment Conventions

Each environment in `platformio.ini` follows this naming convention:

```ini
[env:esp32-desk]        # <board>-<role>
[env:rp2040-hid]
[env:stm32-motor]
[env:uno-node]
[env:native]            # Host-side testing only
```

The `native` environment is used exclusively for unit tests and must not depend on any hardware library.

## Language Strategy

**Primary: C++17** across all four MCU targets. AVR caps at C++14 — avoid C++17+ features in any `lib/` shared code; restrict them to ESP32/RP2040/STM32-specific implementations. Global PlatformIO flags: `-fno-rtti -fno-exceptions`. Heap allocation allowed on ESP32/RP2040; avoid in AVR and ISR contexts.

**Approved split: C++ firmware + Python/TypeScript for host-side tooling and companion app.** PlatformIO is Python-native; OTA scripts, config generators, and sensor dashboards belong there. If a companion app is built, TypeScript (web) or Python are preferred. Protocol serialization shared between firmware and host must be defined once (e.g. via a schema or codegen) — never duplicated by hand.

**Rejected splits and why:**

| Mix | Reason rejected |
|---|---|
| MicroPython on ESP32 | FastLED, LVGL, sensor libs unavailable; breaks shared `lib/` |
| Rust on RP2040 now | `embassy-rp` ecosystem immature; RP2040 becomes an isolated island |
| C++ firmware + C for STM32 HAL | STM32 HAL is C but wraps cleanly in C++ — no reason to split |

Rust on RP2040 is worth revisiting once `embassy` stabilises and the RP2040 role is fully isolated with no shared `lib/` dependencies.

## Coding Style

- Write concise C++: prefer initializer lists, `auto`, range-for, and inline lambdas over verbose equivalents.
- Use established embedded acronyms without expansion: `ISR`, `DMA`, `HAL`, `PWM`, `ADC`, `GPIO`, `MCU`, `OTA`, `NVS`, `IRQ`, `CS`, `SCL`, `SDA`, `MOSI`, `MISO`, `CLK`.
- No filler comments — omit anything a competent reader infers from the code. Only comment non-obvious hardware constraints or protocol quirks.
- Prefer short, precise names: `temp` not `temperatureValue`, `pkt` not `packetBuffer`, `btn` not `buttonState`.
- No defensive no-op error handling; assert or fail fast at boundaries.

## Recommended Libraries

Curated beyond the standard FastLED / TFT_eSPI / Adafruit GFX stack. Prefer these over rolling custom implementations.

### Protocol & Serialization
| Library | Repo | Use |
|---|---|---|
| **EmbeddedProto** | `Embedded-AMS/EmbeddedProto` | Schema-first Protobuf for `DeskProtocol` — zero heap, C++11, AVR-safe |
| **nanopb** | `nanopb/nanopb` | C alternative if AVR C++ codegen is impractical; same `.proto` source |
| **QuickESPNow** | `gmag11/QuickESPNow` | Reliable ESP-NOW unicast/broadcast — sub-ms LED state distribution, no router |
| **esp-matter** | `espressif/esp-matter` | Native Matter/Thread on ESP32-C6/H2 — appears in Apple Home, Google Home, HA simultaneously |

### On-Device ML
| Library | Repo | Use |
|---|---|---|
| **esp-tflite-micro** | `espressif/esp-tflite-micro` | Keyword spotting on mic or gesture classification on IMU, fully on-device |
| **esp-dl** | `espressif/esp-dl` | S3 SIMD-accelerated person/face detection; AutoQuant handles quantization |
| **esp-who** | `espressif/esp-who` | Face-detection pipelines pre-wired to camera for per-person lighting profiles |
| **Edge Impulse** | `edgeimpulse/esp32-platformio-edge-impulse-standalone-example` | Train in cloud → PlatformIO C++ lib output; anomaly block works with no labeled data |
| **tinyml4all-python** | `eloquentarduino/tinyml4all-python` | Exports sklearn models as C++ headers — runs on AVR where TFLite is too heavy |

### RP2040 PIO
| Library | Repo | Use |
|---|---|---|
| **PicoDVI** | `Wren6991/PicoDVI` | Bitbanged DVI/HDMI via 3 PIO state machines — secondary info display, zero external ICs |
| **Pico-PIO-USB** | `sekigon-gonnoc/Pico-PIO-USB` | Second USB port on any GPIO — HID device to PC + USB host simultaneously |
| **HyperSerialPico** | `awawa-dev/HyperSerialPico` | Drives 8 LED strips in parallel via PIO — per-zone desk segments at full sync |
| **pio-i2c-hs** | `tmcqueen-materials/pio-i2c-hs` | PIO I2C at 3.4 Mbps vs 1 Mbps hardware ceiling for dense sensor buses |

### Signal Processing & Sensor Fusion
| Library | Repo | Use |
|---|---|---|
| **CMSIS-DSP** | `ARM-software/CMSIS-DSP` | 4096-point FFT + MFCC on mic buffer; hardware MAC on STM32 |
| **Reefwing-AHRS** | `Reefwing-Software/Reefwing-AHRS` | Six fusion algorithms (Madgwick, Mahony, EKF) for posture detection from IMU |
| **IMU_EKF** | `hobbeshunter/IMU_EKF` | Error-State Kalman Filter with native PlatformIO integration |
| **espp** | `esp-cpp/espp` | 80+ ESP-IDF C++ components: Madgwick, MT6701 encoder, BLE GATT, task abstractions |

### LED Effects & Dev Tools
| Library | Repo | Use |
|---|---|---|
| **WLED (audio reactive)** | `wled/WLED` | 22-band FFT + 100+ reactive effects via I2S mic; accepts DDP/Art-Net from coordinator |
| **wled-sim** | `13rac1/wled-sim` | Desktop WLED REST + DDP simulator — iterate lighting logic without hardware |
| **WLED Studio** | `dolevbs.github.io/wledStudio` | WLED effects engine compiled to WASM — most faithful browser-based preview |

### Presence & Desk Intelligence
| Library | Repo | Use |
|---|---|---|
| **LD2410** | `mgiesen/LD2410` | Engineering-mode per-gate energy — distinguishes seated vs. absent vs. nearby |
| **LD2410Async** | `lizardking/LD2410Async` | FreeRTOS-task-based LD2410 driver — radar in background, main loop unblocked |

### UI & Display
| Library | Repo | Use |
|---|---|---|
| **LVGL + SquareLine Studio** | `lvgl/lvgl` | WYSIWYG UI editor exports `ui.c`/`ui.h` compilable against any LVGL backend |
| **esp32-smartdisplay** | `rzeldent/esp32-smartdisplay` | Pre-wired LVGL drivers for commodity ESP32 TFT + XPT2046 panels |

### Home Automation
| Library | Repo | Use |
|---|---|---|
| **OpenMQTTGateway** | `1technophile/OpenMQTTGateway` | BLE scan → MQTT; decodes 400+ BLE sensor formats without custom per-sensor firmware |
| **aioesphomeapi** | `esphome/aioesphomeapi` | Implement the ESPHome native Protobuf API on ESP32 — sub-10 ms HA updates, no broker |

## Serial Debug Output

Use a consistent log macro rather than raw `Serial.print`. Define log levels via build flags (`-D LOG_LEVEL=2`). The `native` test environment should compile with `LOG_LEVEL=0` to suppress output.

## OTA Updates (ESP32)

The ESP32 environment supports OTA via `ArduinoOTA` or ESP-IDF OTA partitions. The `platformio.ini` OTA upload port/password is set per-environment. Never hard-code Wi-Fi credentials — load from NVS or a `data/config.json` excluded from git.
