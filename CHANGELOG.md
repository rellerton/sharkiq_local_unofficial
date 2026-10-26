# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.4.1] - 2026-10-26

### Changed
- Rebased the development line on Andrew's map, room-cleaning, settings, and
  state-decoding work.
- Pinned the integration to the immutable `sharklocal` 0.3.0.dev14 commit in
  the `rellerton/sharklocal` fork.
- Raised this fork's declared minimum to Home Assistant 2026.10.0 while its
  2026.10 validation and lab soak are completed.

### Fixed
- RV2500AA docked/charging status is decoded from the robot system state rather
  than the unrelated Wi-Fi status field.
- Local REST failures are bounded so MQTT-only robots can finish discovery.
- Windows command-line MQTT discovery uses the compatible selector event loop.

## [0.3.0] - 2026-05-25

### Changed
- **Breaking:** Renamed integration domain from `sharkiq_local` to `sharkiq_local_unofficial` to make the unofficial/community status explicit and clearly separate from any potential future official SharkNinja product.
- Display name updated to "Shark IQ (Local, Unofficial)".

### Migration
- This is effectively a fresh install — remove the old integration and add the new one. Polling-interval settings will need to be reconfigured.

## [0.2.0] - 2026-05-25

### Added
- Configurable polling interval via options flow ("Configure" button on the integration card). Range 5–600 seconds, default 30s.
- Changes apply live — no restart needed.
- Wi-Fi status refresh cadence stays at ~5 min regardless of poll rate, so faster polling doesn't proportionally hammer the heavier `wifi_status` endpoint.

## [0.1.0] - 2026-05-25

### Added
- Initial release
- Config flow for adding multiple Shark IQ vacuums (one entry per vacuum)
- Vacuum entity using new `VacuumActivity` enum (HA 2025.1+)
- Battery sensor (separate, per HA's deprecation of `battery_level` on vacuum entities)
- Diagnostic sensors: Wi-Fi SSID, RSSI, IP address (disabled by default)
- REST + MQTT transport, with REST → MQTT fallback handled by `sharklocal`
- 30-second polling via `DataUpdateCoordinator`
