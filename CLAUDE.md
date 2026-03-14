# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **Home Assistant custom component** for smart EV charging based on electricity spot prices. It replaces a fragile Node-RED flow that polled on timers, hard-coded entity names, and constantly rewrote schedules/restarted charging transactions.

The component selects the cheapest electricity price slots needed to reach the car's per-day target SoC before a configured departure time, and adjusts charging current dynamically to stay within household breaker limits — all without restarting charging sessions.

## Repository Structure

```
custom_components/
  goe_cheap_charging/     ← the integration lives here
    __init__.py
    config_flow.py       ← UI-driven setup, no hard-coded values
    coordinator.py       ← core scheduling + amp-adjustment logic
    charger/
      goe.py             ← Go-e charger driver (MQTT only)
    car/
      kia_uwo.py         ← Kia UVO car driver
    const.py
    manifest.json
```

## Development Commands

```bash
# Run tests
python -m pytest tests/ -v

# Run a single test file
python -m pytest tests/test_coordinator.py -v

# Lint
ruff check custom_components/
```

For manual testing: copy `custom_components/goe_cheap_charging/` into your HA `config/custom_components/` directory and restart HA. Check logs at **Settings → System → Logs**.

## Architecture

### Config Flow (no hard-coding)

All configuration is done via the HA GUI when adding the integration. The config flow collects only the static, structural parameters that cannot be changed at runtime:
- **Charger**: select from discovered Go-e devices (entity picker filtered by the Go-e MQTT integration's entities)
- **Car**: select from discovered Kia UVO vehicle entities (integration domain `kia_uvo`)
- **Energy monitor entities**: L1/L2/L3 current sensors (any `sensor` entity with `unit_of_measurement: A`)
- **Battery capacity** (kWh), **charge efficiency** (%), **breaker limit** (A), **which phase the charger is on** (1/2/3), **min/max charge amps**

Everything that a user might want to adjust on the fly is **created as HA entities by the integration itself** — no manually created helpers are ever required. This includes per-day settings and overrides (see Entities section below).

### Entities Created by the Integration

After setup the integration registers the following entities so they can be placed on HA dashboards:

**Per weekday** (monday–sunday):
- `switch.cheap_charging_{day}_enabled` — whether this day has a departure
- `time.cheap_charging_{day}_departure` — time-of-day picker (HH:MM)
- `number.cheap_charging_{day}_target_soc` — target battery % for this day

**Global controls:**
- `switch.cheap_charging_smart_enabled` — master on/off for smart charging
- `switch.cheap_charging_charge_now` — manual override (charge immediately regardless of price)

**Status (read-only sensors):**
- `sensor.cheap_charging_schedule` — human-readable summary of the current charging plan
- `sensor.cheap_charging_next_slot` — start time of next selected price slot

### Coordinator (core logic)

`DataUpdateCoordinator` subclass that reacts to state changes rather than polling timers.

**Schedule building** — triggered when any of these change: price data, car SoC, departure time/enabled, target SoC, smart-enabled toggle, or on plug-in:
1. Determines next enabled departure day (today or tomorrow) and reads its `target_soc` and `departure` time entities.
2. Calls `nordpool.get_price_indices_for_date` for today's date and tomorrow's date separately to get price slots.
3. Filters slots to those that are in the future and before the departure time.
4. Calculates kWh needed: `(target_soc - current_soc) / 100 * capacity_kwh / efficiency`.
5. Selects the N cheapest 15-min slots. Slot count: `ceil(kwh_needed / (max_charge_kw * 0.25))`.
6. Stores the schedule in memory — does **not** rewrite it on every tick.

If tomorrow's prices are not yet available (the nordpool action returns no data for tomorrow's date), schedule building for overnight departures is deferred. Re-attempt after 13:30 local time, since Nordpool publishes next-day prices around that time.

**Charger control** — triggered by schedule changes or when crossing a slot boundary:
- If currently in a selected slot (or override active): set `frc=2`.
- Outside a selected slot: set `frc=1` (force stop / pause).
- Commands are sent via MQTT only (see Go-e driver below).

**Amp adjustment** — runs every 30 s only while car state is `2` (charging):
1. Reads L1/L2/L3 currents from configured energy monitor entities.
2. Subtracts the charger's current contribution from the charger's phase to get household baseline.
3. Headroom = `breaker_limit - max(all_phase_baselines)`.
4. New amp = clamp(headroom, min_amp, max_amp). Only sends command if change ≥ 1 A.

### Charger Driver — Go-e

The Go-e charger is integrated into HA via the **go-e MQTT integration** (configured in the go-e app). This integration uses MQTT autodiscovery to create HA sensor/switch entities from the charger's retained MQTT status topic `go-eCharger/{serial}/status`.

**Car states** (from `car` key in MQTT status):
- `1` = idle (no cable)
- `2` = charging
- `3` = connected / waiting (cable in, not charging)
- `4` = charge complete

**Control** — publish JSON to `go-eCharger/{serial}/cmd/set` via HA's `mqtt.publish` service. Never use HTTP API; all control goes through MQTT. Example payloads:
```json
{"frc": 2}          // force charge on (resume)
{"frc": 1}          // force stop (pause)
{"amp": 10}         // set charging current in amps
{"trx": 1}          // start a new transaction
{"frc": 2, "trx": 1}  // start transaction and force charge simultaneously
```

**Transaction management** — critical rules:
- On first plug-in (state transition `1→3`): send `{"trx": 1, "frc": 2}` if in a cheap slot, or `{"trx": 1, "frc": 1}` to start paused.
- Between price slots during the same session: use only `frc=1` / `frc=2` to pause/resume. **Never send `trx` again** while a session is in progress.
- Before sending `trx=1`, check if `car` state is `3` AND there is already an active/paused transaction (check the `trx` key in charger status). If a transaction is already running, skip `trx=1` and only set `frc`.
- On charge complete (state→`4`): clear the transaction flag so the next plug-in starts fresh.

**On plug-in**: call `kia_uvo.force_update` immediately, then wait 60 s before rebuilding the schedule (to allow the UVO cloud to return updated SoC).

### Car Driver — Kia UVO

- Reads current SoC from the configured `sensor` entity (numeric %).
- Before building the schedule, calls `kia_uvo.force_update` (passing `device_id` from the config) to get a fresh SoC reading.
- Before charging begins, calls the kia_uwo service to set the car's **maximum charge limit** to the day's `target_soc` value, so the car itself stops at the right level as a safety net.
- Calls `kia_uvo.force_update` once per hour while charging to keep SoC current for schedule recalculation.

### Energy Monitoring

Uses standard HA `sensor` entities for phase currents (unit `A`). The user picks any three entities in the config flow — Tibber Pulse, Shelly EM, or any other meter works without vendor-specific code.

### Electricity Prices — Nordpool

Uses the official **Nordpool integration** action `nordpool.get_price_indices_for_date` to fetch price data. Call this action separately for today and tomorrow:

```yaml
service: nordpool.get_price_indices_for_date
data:
  date: "2024-01-15"   # ISO date string, today or tomorrow
```

The action returns a list of price entries. If calling for tomorrow returns an empty result or an error, tomorrow's prices are not yet published — retry after 13:30 local time. Do not rely on any `tomorrow_valid` flag; instead check whether the returned data for tomorrow's date is non-empty.

## Key Design Rules

- **No session restarts**: `frc` and `amp` are adjusted in-session. The only time `trx=1` is sent is when initiating the very first charge after a new plug-in event.
- **Event-driven scheduling**: rebuild the schedule only when a relevant input changes. Use `async_track_state_change_event` for all watched entities.
- **Integration creates all runtime entities**: users never create `input_boolean`, `input_number`, or `input_datetime` helpers manually. All adjustable settings are HA entities registered by this integration.
- **MQTT only for Go-e control**: do not implement an HTTP fallback. If something cannot be done via the MQTT command topic, document it as a known limitation rather than silently switching protocols.
- **Amp adjustment only while charging**: skip the 30 s loop entirely when car state is not `2`.
- **Per-day target SoC**: each weekday has its own target SoC entity. The car's hardware charge limit is also set via kia_uwo before charging starts to match the day's target.
- **Tomorrow price retry**: if an overnight departure exists but tomorrow's Nordpool data is unavailable, schedule a retry check after 13:30. Do not permanently defer or error out.
