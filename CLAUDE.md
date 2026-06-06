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

### Hardware Abstraction Layer (HAL)
All hardware-specific code lives behind interfaces defined in `include/`. Concrete implementations go in `lib/`. Tests use mock implementations. Never call platform-specific APIs (e.g., `digitalWrite`, `gpio_set_level`) directly in business logic — wrap them.

### Inter-MCU Protocol
MCUs communicate over UART or I2C using a shared packet format defined in `lib/DeskProtocol/`. The ESP32 acts as the bus master. New message types require updating the protocol definition and all listening nodes.

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

## Serial Debug Output

Use a consistent log macro rather than raw `Serial.print`. Define log levels via build flags (`-D LOG_LEVEL=2`). The `native` test environment should compile with `LOG_LEVEL=0` to suppress output.

## OTA Updates (ESP32)

The ESP32 environment supports OTA via `ArduinoOTA` or ESP-IDF OTA partitions. The `platformio.ini` OTA upload port/password is set per-environment. Never hard-code Wi-Fi credentials — load from NVS or a `data/config.json` excluded from git.
