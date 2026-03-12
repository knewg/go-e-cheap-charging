# EV Smart Charging

A Home Assistant custom component that charges your EV during the cheapest electricity price slots while respecting household breaker limits — without ever restarting the charging session.

## How it works

When the car is plugged in, the component calculates how many 1-hour price buckets are needed to reach the day's target SoC before departure. It selects the cheapest buckets in that window and stores a schedule. At each slot boundary it sends a `frc=2` (resume) or `frc=1` (pause) command to the Go-e charger via MQTT. Every 30 seconds while charging, it reads L1/L2/L3 phase currents and trims or raises the charging current to stay within the household breaker limit — no session restarts, only in-session `amp` adjustments.

If the price spread across the window is below the configured threshold, all slots are selected and the car charges continuously at the cheapest possible time rather than cherry-picking isolated hours.

## Prerequisites

### Home Assistant integrations

| Integration | Purpose |
|---|---|
| [MQTT](https://www.home-assistant.io/integrations/mqtt/) | Control Go-e charger |
| [Nordpool](https://github.com/custom-components/nordpool) | Electricity spot prices |
| [Kia UVO](https://github.com/Hyundai-Kia-Connect/kia_uvo) | Car SoC and charge limit |

### Hardware

- Go-e charger with MQTT enabled (configured in the go-e app)
- Kia EV (or other Hyundai/Kia vehicle supported by Kia UVO)
- Phase current energy meter with HA sensor entities (unit: A) — Tibber Pulse, Shelly EM, or similar

## Installation

1. Copy `custom_components/ev_smart_charging/` into your HA `config/custom_components/` directory.
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration** and search for **EV Smart Charging**.
4. Complete the three-step config flow (see [Configuration](#configuration) below).
5. Place the created entities on your dashboard.

## Configuration

The config flow has three steps. All values can be changed at any time through the entities the integration creates.

### Step 1 — Charger and car

| Field | Description |
|---|---|
| Charger serial | Go-e charger serial number (used to build MQTT topic) |
| Car SoC entity | Kia UVO battery level sensor |
| Car device ID | Kia UVO device ID (for `force_update` calls) |

### Step 2 — Phase current sensors

Select three sensor entities (unit: A) for L1, L2, and L3. Any HA-compatible energy meter works.

### Step 3 — Battery and charger parameters

| Parameter | Default | Range |
|---|---|---|
| Battery capacity (kWh) | 64.0 | — |
| Charge efficiency | 0.90 | 0.5 – 1.0 |
| Breaker limit (A) | 20 | 10 – 63 |
| Charger phase | 1 | 1 / 2 / 3 |
| Min charge amps | 6 | 6 – 32 |
| Max charge amps | 16 | 6 – 32 |

## Entities created by the integration

### Per weekday (monday – sunday, 21 entities total)

| Entity ID pattern | Type | Description |
|---|---|---|
| `switch.ev_charging_{day}_enabled` | Switch | Enable/disable departure for this day |
| `time.ev_charging_{day}_departure` | Time | Departure time (HH:MM) |
| `number.ev_charging_{day}_target_soc` | Number (slider, 20–100 %) | Target battery % for this day |

### Global controls

| Entity ID | Type | Description |
|---|---|---|
| `switch.ev_charging_smart_enabled` | Switch | Master on/off for smart charging |
| `switch.ev_charging_charge_now` | Switch | Override: charge immediately regardless of price |

### Thresholds

| Entity ID | Type | Default | Description |
|---|---|---|---|
| `number.ev_charging_cheap_price_threshold` | Number (SEK/kWh) | 0.00 | Slots at or below this price are always included (0 = disabled) |
| `number.ev_charging_price_spread_threshold` | Number (SEK/kWh) | 0.10 | If max − min price in window is below this, charge the full window continuously |

### Status sensors (read-only)

| Entity ID | Description |
|---|---|
| `sensor.ev_charging_schedule` | Human-readable summary of the current charging plan |
| `sensor.ev_charging_next_slot` | Timestamp of the next selected price slot |

## Charging logic

### Schedule building

Triggered whenever price data, car SoC, departure settings, or the smart-enabled switch changes, and on plug-in.

1. Find the next enabled departure day and read its `target_soc` and `departure` entities.
2. Fetch Nordpool price slots for today and (if needed) tomorrow.
3. Filter to slots that are in the future and before the departure time.
4. Calculate energy needed: `(target_soc − current_soc) / 100 × capacity_kWh / efficiency`.
5. Determine how many 1-hour buckets are needed using a conservative 10 A planning current.
6. Apply spread check:

| Condition | Behaviour |
|---|---|
| Price spread < threshold | Select all slots (charge continuously) |
| Price spread >= threshold | Select the N cheapest 1-hour buckets; fill any single-slot gaps between selected buckets |

If tomorrow's prices are not yet available, schedule building is deferred and retried after 13:30 local time (Nordpool publishes next-day prices around that time).

### Charger control

- **In a selected slot** (or `charge_now` override active): send `frc=2` to resume.
- **Outside a selected slot**: send `frc=1` to pause.
- `trx=1` (start transaction) is sent **only once per plug-in event**, never again during the session.

### Amp adjustment

Runs every 30 seconds while the car is in state `2` (charging):

1. Read L1, L2, L3 current sensors.
2. Subtract the charger's contribution from the phase it is on to get the household baseline per phase.
3. Headroom = `breaker_limit − max(all_phase_baselines)`.
4. New amp = clamp(headroom, min_amp, max_amp). Command is sent only if the change is ≥ 1 A.

## Development

```bash
# Run tests
python3 -m pytest tests/ -v

# Run a single test file
python3 -m pytest tests/test_coordinator.py -v

# Lint
ruff check custom_components/
```

For manual testing: copy `custom_components/ev_smart_charging/` into your HA `config/custom_components/` directory and restart HA. Logs are at **Settings → System → Logs**.
