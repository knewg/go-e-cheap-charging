# GO-e Cheap Charging

A Home Assistant custom component that charges your EV during the cheapest electricity price slots while respecting household breaker limits — without ever restarting the charging session.

## How it works

When the car is plugged in, the component calculates how many 15-minute price slots are needed to reach the day's target SoC before departure. It selects the cheapest slots in that window and stores a schedule. At each slot boundary it sends a `frc=2` (resume) or `frc=1` (pause) command to the Go-e charger via MQTT. Every 30 seconds while charging, it reads L1/L2/L3 phase currents and trims or raises the charging current to stay within the household breaker limit — no session restarts, only in-session `amp` adjustments.

If the price spread across the window is below the configured threshold, all slots are selected and the car charges continuously rather than cherry-picking isolated slots.

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

1. Copy `custom_components/goe_cheap_charging/` into your HA `config/custom_components/` directory.
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration** and search for **GO-e Cheap Charging**.
4. Complete the config flow (see [Configuration](#configuration) below).
5. Place the created entities on your dashboard.

## Configuration

The config flow has two steps (three if the charger is single-phase). All values can be changed at any time through the entities the integration creates, or by using **Reconfigure** on the integration.

### Step 1 — Charger and battery parameters

| Field | Default | Description |
|---|---|---|
| Charger serial | — | Go-e charger serial number (auto-discovered from go-e MQTT entities, or entered manually) |
| Battery capacity (kWh) | 64.0 | Usable battery size |
| Charge efficiency | 90 % | AC→battery efficiency (50–100 %) |
| Breaker limit (A) | 20 | Household main breaker per phase |
| Number of phases | 3 | `1` for single-phase charger, `3` for three-phase |
| Min charge amps | 6 | Lower bound for amp adjustment (6–32 A) |
| Max charge amps | 16 | Upper bound for amp adjustment (6–32 A) |

### Step 1b — Charger phase *(single-phase only)*

If **Number of phases** is `1`, an extra step asks which household phase (1, 2, or 3) the charger is wired to. This is needed so the amp-adjustment logic can correctly subtract the charger's contribution when calculating household headroom.

### Step 2 — Electrical sensors

| Field | Description |
|---|---|
| L1 current sensor | HA sensor entity (unit: A) for phase 1 |
| L2 current sensor | HA sensor entity (unit: A) for phase 2 |
| L3 current sensor | HA sensor entity (unit: A) for phase 3 |
| Transit cost sensor *(optional)* | Price-per-kWh sensor for grid transit fees (added to spot price in schedule display) |

## Entities created by the integration

### Car selection

| Entity ID | Type | Description |
|---|---|---|
| `select.cheap_charging_active_car` | Select | Choose which Kia UVO vehicle to charge, or **Guest** mode (no SoC-based scheduling) |

The dropdown is auto-populated from discovered Kia UVO devices. In **Guest** mode no SoC reading is attempted and per-day target SoC is ignored.

### Per weekday (monday – sunday, 28 entities total)

| Entity ID pattern | Type | Default | Description |
|---|---|---|---|
| `switch.cheap_charging_{day}_enabled` | Switch | off | Enable/disable departure scheduling for this day |
| `time.cheap_charging_{day}_departure` | Time | 07:00 | Departure time (HH:MM) |
| `number.cheap_charging_{day}_target_soc` | Number (slider, 0–100 %) | 80 | Target battery % for this day |
| `number.cheap_charging_{day}_manual_kwh` | Number (slider, 0–100 kWh) | 0.0 | Manual kWh override; if > 0, replaces SoC-based energy calculation |

### Global controls

| Entity ID | Type | Default | Description |
|---|---|---|---|
| `switch.cheap_charging_smart_enabled` | Switch | on | Master on/off for smart charging |
| `switch.cheap_charging_charge_now` | Switch | off | Override: charge immediately regardless of price (resets to off on HA restart) |

### Thresholds and limits

| Entity ID | Type | Default | Description |
|---|---|---|---|
| `number.cheap_charging_cheap_price_threshold` | Number (SEK/kWh) | 0.00 | Slots at or below this price are always included regardless of schedule (0 = disabled) |
| `number.cheap_charging_price_spread_threshold` | Number (SEK/kWh) | 0.10 | If max − min price in window is below this, charge the full window continuously |
| `number.cheap_charging_opportunistic_soc_limit` | Number (slider, 0–100 %) | 80 | SoC cap for opportunistic cheap-price charging |
| `number.cheap_charging_charge_now_soc_limit` | Number (slider, 0–100 %) | 80 | SoC cap when **Charge Now** override is active |

### Status sensors (read-only)

| Entity ID | Description |
|---|---|
| `sensor.cheap_charging_schedule` | Human-readable summary of the current charging plan |
| `sensor.cheap_charging_next_slot` | Timestamp of the next selected price slot |

## Charging logic

### Schedule building

Triggered whenever price data, car SoC, departure settings, the smart-enabled switch, or the active car selection changes, and on plug-in.

1. Find the next enabled departure day and read its `target_soc`, `manual_kwh`, and `departure` entities.
2. Fetch Nordpool price slots for today and (if needed) tomorrow.
3. Filter to slots that are in the future and before the departure time.
4. Calculate energy needed:
   - If `manual_kwh` > 0: use that value directly.
   - Otherwise: `(target_soc − current_soc) / 100 × capacity_kWh / efficiency`.
5. Determine how many 15-minute slots are needed using a planning current of 10 A.
6. Apply spread and threshold checks:

| Condition | Behaviour |
|---|---|
| Price spread < spread threshold | Select all slots (charge continuously) |
| Slot price ≤ cheap price threshold | Always include slot (opportunistic), subject to `opportunistic_soc_limit` |
| Otherwise | Select the N cheapest 15-minute slots; fill any single-slot gaps between selected slots |

If tomorrow's prices are not yet available, schedule building is deferred and retried after 13:30 local time (Nordpool publishes next-day prices around that time).

### Charger control

- **In a selected slot** (or `charge_now` override active): send `frc=2` to resume.
- **Outside a selected slot**: send `frc=1` to pause.
- `trx=1` (start transaction) is sent **only once per plug-in event**, never again during the session.
- `charge_now` is subject to `charge_now_soc_limit` — charging stops if the current SoC meets or exceeds the limit.

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

For manual testing: copy `custom_components/goe_cheap_charging/` into your HA `config/custom_components/` directory and restart HA. Logs are at **Settings → System → Logs**.
