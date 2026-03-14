"""Core coordinator: scheduling, charger control, amp adjustment."""
from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import date, datetime, time, timedelta
from typing import Any

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .car.kia_uvo import KiaUvoDriver
from .charger.goe import GoeCharger
from .const import (
    ACTIVE_CAR_GUEST,
    AMP_ADJUST_INTERVAL_S,
    CAR_CHARGING,
    CAR_COMPLETE,
    CAR_CONNECTED,
    CAR_IDLE,
    CONF_BATTERY_CAPACITY,
    CONF_BREAKER_LIMIT,
    CONF_CHARGER_N_PHASES,
    CONF_CHARGER_PHASE,
    CONF_CHARGER_SERIAL,
    CONF_EFFICIENCY,
    CONF_MAX_AMP,
    CONF_MIN_AMP,
    CONF_PHASE_L1_ENTITY,
    CONF_PHASE_L2_ENTITY,
    CONF_PHASE_L3_ENTITY,
    DEFAULT_CHARGE_NOW_SOC_LIMIT,
    DEFAULT_CHARGER_N_PHASES,
    DEFAULT_CHEAP_THRESHOLD,
    DEFAULT_MANUAL_KWH,
    DEFAULT_OPPORTUNISTIC_SOC_LIMIT,
    DEFAULT_PRICE_SPREAD_THRESHOLD,
    DEFAULT_TARGET_SOC,
    DOMAIN,
    NORDPOOL_PRICES_AVAILABLE_HOUR,
    NORDPOOL_PRICES_AVAILABLE_MINUTE,
    PLUGIN_DELAY_S,
    SCHEDULE_PLANNING_AMP,
    WEEKDAYS,
)

_LOGGER = logging.getLogger(__name__)

MIN_BLOCK_SLOTS = 4  # minimum contiguous 15-min slots per charging block (= 1 hour)


def _get_clusters(selected: set, n: int) -> list:
    """Return list of contiguous index runs that are in *selected*."""
    clusters: list = []
    current: list = []
    for i in range(n):
        if i in selected:
            current.append(i)
        else:
            if current:
                clusters.append(current)
                current = []
    if current:
        clusters.append(current)
    return clusters


def _select_slots(slots: list, n_slots: int, spread_threshold: float) -> None:
    """Select cheapest slots in-place, enforcing ≥1-hour blocks and cheap-gap filling.

    Algorithm:
      1. Compute Option A: cheapest single contiguous block of n_needed slots.
      2. Compute Option B: greedy cheapest non-overlapping MIN_BLOCK_SLOTS windows.
      3. Prefer Option A (single block) unless Option B saves > spread_threshold per kWh.
      4. If multi-block is chosen, fill cheap gaps using average price as reference
         (not max, to avoid expensive mandatory windows inflating the threshold).
    """
    n = len(slots)
    n_slots = min(n_slots, n)
    if n_slots == 0:
        return

    # Fewer slots than the minimum block — just select all
    if n < MIN_BLOCK_SLOTS:
        for s in slots:
            s["selected"] = True
        return

    # Round up n_needed to the nearest full block
    n_needed = math.ceil(n_slots / MIN_BLOCK_SLOTS) * MIN_BLOCK_SLOTS
    n_needed = min(n_needed, n)

    # Option A: cheapest single contiguous block of n_needed slots
    best_start = min(
        range(n - n_needed + 1),
        key=lambda i: sum(slots[j]["price"] for j in range(i, i + n_needed)),
    )
    single_avg = sum(slots[j]["price"] for j in range(best_start, best_start + n_needed)) / n_needed

    # Option B: greedy cheapest non-overlapping MIN_BLOCK_SLOTS windows
    windows = sorted(
        range(n - MIN_BLOCK_SLOTS + 1),
        key=lambda i: sum(slots[j]["price"] for j in range(i, i + MIN_BLOCK_SLOTS)),
    )
    multi_selected: set = set()
    for start in windows:
        if len(multi_selected) >= n_needed:
            break
        indices = set(range(start, start + MIN_BLOCK_SLOTS))
        if not indices & multi_selected:
            multi_selected |= indices
    multi_avg = (
        sum(slots[i]["price"] for i in multi_selected) / len(multi_selected)
        if multi_selected else float("inf")
    )

    # Prefer single block (fewer start/stops) unless multi-block saves > threshold
    if single_avg <= multi_avg + spread_threshold:
        selected = set(range(best_start, best_start + n_needed))
    else:
        selected = multi_selected
        # Gap-fill: use average price of selected slots as reference to avoid
        # expensive mandatory windows inflating the threshold
        ref_price = sum(slots[i]["price"] for i in selected) / len(selected)
        changed = True
        while changed:
            changed = False
            clusters = _get_clusters(selected, n)
            for j in range(len(clusters) - 1):
                gap = list(range(clusters[j][-1] + 1, clusters[j + 1][0]))
                if gap and all(
                    slots[i]["price"] <= ref_price + spread_threshold for i in gap
                ):
                    for i in gap:
                        selected.add(i)
                    ref_price = max(
                        ref_price,
                        sum(slots[i]["price"] for i in gap) / len(gap),
                    )
                    changed = True

    for i, s in enumerate(slots):
        s["selected"] = i in selected


class ChargingCoordinator(DataUpdateCoordinator):
    """Manages smart charging schedule and charger control."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN)

        cfg = entry.data
        self._entry = entry
        self._serial: str = cfg[CONF_CHARGER_SERIAL]
        self._battery_capacity: float = cfg[CONF_BATTERY_CAPACITY]
        self._efficiency: float = cfg[CONF_EFFICIENCY]
        self._breaker_limit: int = cfg[CONF_BREAKER_LIMIT]
        self._charger_phase: int = cfg[CONF_CHARGER_PHASE]
        self._charger_n_phases: int = cfg.get(CONF_CHARGER_N_PHASES, DEFAULT_CHARGER_N_PHASES)
        self._min_amp: int = cfg[CONF_MIN_AMP]
        self._max_amp: int = cfg[CONF_MAX_AMP]

        self._phase_entities: list[str] = [
            cfg[CONF_PHASE_L1_ENTITY],
            cfg[CONF_PHASE_L2_ENTITY],
            cfg[CONF_PHASE_L3_ENTITY],
        ]

        self.charger = GoeCharger(hass, self._serial)
        self.car: KiaUvoDriver | None = None
        self._active_car_is_guest: bool = False
        self._soc_unsub: Any = None

        # Runtime state
        self.car_state: int = CAR_IDLE
        self.schedule: list[dict] = []
        self._transaction_active: bool = False
        self._last_sent_amp: int = 0
        self._smart_enabled: bool = False
        self._charge_now: bool = False
        self._cheap_threshold: float = DEFAULT_CHEAP_THRESHOLD
        self._opportunistic_soc_limit: float = DEFAULT_OPPORTUNISTIC_SOC_LIMIT
        self._charge_now_soc_limit: float = DEFAULT_CHARGE_NOW_SOC_LIMIT
        self._last_sent_car_limit: int | None = None
        self._price_spread_threshold: float = DEFAULT_PRICE_SPREAD_THRESHOLD
        self._current_price_data: list[dict] = []

        # Debug / status fields
        self._schedule_status_reason: str = "Initializing..."
        self._last_kwh_needed: float = 0.0
        self._last_current_soc: float | None = None
        self._last_target_soc: int = 0
        self._next_departure_dt: datetime | None = None

        # Per-day settings: {day: {"departure": time|None, "target_soc": int, "manual_kwh": float}}
        self._day_settings: dict[str, dict] = {
            day: {"departure": None, "target_soc": DEFAULT_TARGET_SOC, "manual_kwh": DEFAULT_MANUAL_KWH}
            for day in WEEKDAYS
        }

        # Cancellation handles
        self._mqtt_unsub: Any = None
        self._state_unsubs: list[Any] = []
        self._amp_adjust_task: asyncio.Task | None = None
        self._hourly_update_task: asyncio.Task | None = None
        self._tomorrow_retry_cancel: Any = None
        self._pending_rebuild_cancel: Any = None
        self._slot_timer_cancel: Any = None

        # References to sensor entities for pushing state updates
        self._schedule_sensor: Any = None
        self._next_slot_sensor: Any = None

    # ------------------------------------------------------------------
    # Setup / teardown
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Subscribe to MQTT and state-change events. Called from __init__.py."""
        # MQTT subscription for charger status
        self._mqtt_unsub = await mqtt.async_subscribe(
            self.hass,
            self.charger.status_topic,
            self._handle_mqtt_message,
            qos=0,
        )

        # SoC entity watcher (kept separately so it can be rewired on car change)
        # Use _rewire_soc_watcher() to avoid leaking any subscription already
        # created by select.py's async_added_to_hass() → async_set_active_car()
        self._rewire_soc_watcher()

        # Entities that should trigger a schedule rebuild
        entities_to_watch: list[str] = []
        for day in WEEKDAYS:
            entities_to_watch.extend(
                [
                    self._entity_id("time", f"{day}_departure"),
                    self._entity_id("number", f"{day}_target_soc"),
                    self._entity_id("number", f"{day}_manual_kwh"),
                ]
            )
        entities_to_watch.append(self._entity_id("switch", "smart_enabled"))

        self._state_unsubs.append(
            async_track_state_change_event(
                self.hass,
                entities_to_watch,
                self._handle_state_change,
            )
        )

        # charge_now override triggers charger command directly
        self._state_unsubs.append(
            async_track_state_change_event(
                self.hass,
                [self._entity_id("switch", "charge_now")],
                self._handle_charge_now_change,
            )
        )

        # cheap price threshold change — re-evaluate charger command only
        self._state_unsubs.append(
            async_track_state_change_event(
                self.hass,
                [self._entity_id("number", "cheap_price_threshold")],
                self._handle_threshold_change,
            )
        )

        # price spread threshold change — affects schedule selection
        self._state_unsubs.append(
            async_track_state_change_event(
                self.hass,
                [self._entity_id("number", "price_spread_threshold")],
                self._handle_spread_threshold_change,
            )
        )

        # SoC limit changes — re-evaluate charger command only
        self._state_unsubs.append(
            async_track_state_change_event(
                self.hass,
                [self._entity_id("number", "opportunistic_soc_limit")],
                self._handle_opportunistic_soc_limit_change,
            )
        )
        self._state_unsubs.append(
            async_track_state_change_event(
                self.hass,
                [self._entity_id("number", "charge_now_soc_limit")],
                self._handle_charge_now_soc_limit_change,
            )
        )

        if self.hass.state == CoreState.running:
            # Integration reload: entities will restore and call schedule_pending_rebuild().
            pass
        else:
            # HA startup: wait until fully started so Nordpool and all entities are ready.
            @callback
            def _on_ha_started(event: Any) -> None:
                if self._pending_rebuild_cancel:
                    self._pending_rebuild_cancel()
                    self._pending_rebuild_cancel = None
                if self._tomorrow_retry_cancel:
                    self._tomorrow_retry_cancel()
                    self._tomorrow_retry_cancel = None
                self.hass.async_create_task(self._async_rebuild_schedule())

            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_ha_started)

    def _schedule_next_slot_timer(self) -> None:
        """Set a callback to fire at the next selected slot boundary (start or end).

        Fires at both starts and ends of selected slots so _async_apply_charger_command
        can correctly send frc=2 when a slot begins and frc=1 when a gap begins.
        """
        if self._slot_timer_cancel:
            self._slot_timer_cancel()
            self._slot_timer_cancel = None

        now = dt_util.now()
        # Collect all future boundary times for selected slots
        boundaries: set = set()
        for s in self.schedule:
            if s["selected"]:
                if s["start"] > now:
                    boundaries.add(s["start"])
                if s["end"] > now:
                    boundaries.add(s["end"])

        if not boundaries:
            return

        next_boundary = min(boundaries)
        delay = (next_boundary - now).total_seconds()

        @callback
        def _on_slot_boundary(_now: Any) -> None:
            self._slot_timer_cancel = None
            self.hass.async_create_task(self._async_on_slot_boundary())

        self._slot_timer_cancel = async_call_later(self.hass, delay, _on_slot_boundary)
        _LOGGER.debug(
            "Next slot boundary timer set for %s (%.0fs)",
            dt_util.as_local(next_boundary).strftime("%H:%M"),
            delay,
        )

    async def _async_on_slot_boundary(self) -> None:
        """Called when a scheduled slot starts or ends."""
        _LOGGER.debug("Slot boundary fired — re-evaluating charger command")
        await self._async_apply_charger_command()
        self._schedule_next_slot_timer()

    def schedule_pending_rebuild(self) -> None:
        """Schedule a rebuild in 1s, debouncing rapid calls from entity restore."""
        if self._pending_rebuild_cancel:
            self._pending_rebuild_cancel()

        @callback
        def _do_rebuild(_now: Any) -> None:
            self._pending_rebuild_cancel = None
            self.hass.async_create_task(self._async_rebuild_schedule())

        self._pending_rebuild_cancel = async_call_later(self.hass, 1, _do_rebuild)

    async def async_shutdown(self) -> None:
        """Unsubscribe everything and cancel tasks."""
        if self._mqtt_unsub:
            self._mqtt_unsub()
        if self._soc_unsub:
            self._soc_unsub()
        for unsub in self._state_unsubs:
            unsub()
        self._state_unsubs.clear()
        if self._amp_adjust_task and not self._amp_adjust_task.done():
            self._amp_adjust_task.cancel()
        self._stop_hourly_force_update()
        if self._tomorrow_retry_cancel:
            self._tomorrow_retry_cancel()
        if self._pending_rebuild_cancel:
            self._pending_rebuild_cancel()
        if self._slot_timer_cancel:
            self._slot_timer_cancel()

    # ------------------------------------------------------------------
    # Active car management
    # ------------------------------------------------------------------

    def async_set_active_car(self, soc_entity_id: str, device_id: str) -> None:
        """Switch the active car. Called by the select entity."""
        if soc_entity_id == ACTIVE_CAR_GUEST:
            self._active_car_is_guest = True
        else:
            self._active_car_is_guest = False
            self.car = KiaUvoDriver(self.hass, device_id)
        self._rewire_soc_watcher()

    def _rewire_soc_watcher(self) -> None:
        if self._soc_unsub:
            self._soc_unsub()
            self._soc_unsub = None
        if not self._active_car_is_guest and self.car is not None:
            soc_entity = self.car.soc_entity_id
            if soc_entity is not None:
                self._soc_unsub = async_track_state_change_event(
                    self.hass, [soc_entity], self._handle_state_change
                )

    # ------------------------------------------------------------------
    # Entity ID helpers
    # ------------------------------------------------------------------

    def _entity_id(self, platform: str, suffix: str) -> str:
        return f"{platform}.{DOMAIN}_{suffix}"

    # ------------------------------------------------------------------
    # MQTT handler
    # ------------------------------------------------------------------

    @callback
    def _handle_mqtt_message(self, msg: Any) -> None:
        _LOGGER.debug("MQTT ← %s : %s", msg.topic, msg.payload)
        key = self.charger.extract_key(msg.topic)
        if key is None:
            return

        try:
            value = json.loads(msg.payload)
        except (json.JSONDecodeError, TypeError):
            return

        if key == "trx":
            _LOGGER.debug("MQTT trx update: %s → transaction_active=%s", value, bool(value))
            self._transaction_active = bool(value)
        elif key == "car":
            self._handle_car_state(int(value))

    def _handle_car_state(self, new_car_state: int) -> None:
        prev = self.car_state
        self.car_state = new_car_state

        _LOGGER.info(
            "Car state: %s → %s  (transaction_active=%s)",
            prev, new_car_state, self._transaction_active,
        )

        if prev == CAR_IDLE and new_car_state in (CAR_CONNECTED, CAR_CHARGING):
            if self._transaction_active:
                _LOGGER.info(
                    "Recovered ongoing session on startup (car=%s) — applying schedule",
                    new_car_state,
                )
                self.hass.async_create_task(self._async_apply_charger_command())
            elif new_car_state == CAR_CONNECTED:
                self.hass.async_create_task(self._async_handle_plugin())
            else:
                # car=2 but no trx yet — mark active (trx message may arrive separately)
                _LOGGER.info("car=2 on startup, marking transaction active")
                self._transaction_active = True

        elif new_car_state == CAR_COMPLETE and prev != CAR_COMPLETE:
            _LOGGER.debug("CAR_COMPLETE: clearing _last_sent_car_limit and transaction")
            self._transaction_active = False
            self._last_sent_car_limit = None   # resend limit if car resumes after target SoC increase
            self._stop_amp_adjust()
            _LOGGER.info("Charge complete")

        # Start/stop amp-adjust loop
        if new_car_state == CAR_CHARGING and (
            self._amp_adjust_task is None or self._amp_adjust_task.done()
        ):
            self._amp_adjust_task = self.hass.async_create_task(
                self._async_amp_adjust_loop()
            )
        elif new_car_state != CAR_CHARGING:
            self._stop_amp_adjust()

        # Force update and hourly loop management on charging state transitions
        if new_car_state == CAR_CHARGING and prev != CAR_CHARGING:
            _LOGGER.info(
                "Charging started — force update queued, long_block=%s",
                self._is_long_charging_block(),
            )
            if not self._active_car_is_guest and self.car is not None:
                self.hass.async_create_task(self.car.async_force_update())
            if self._is_long_charging_block():
                self._start_hourly_force_update()
        elif prev == CAR_CHARGING and new_car_state != CAR_CHARGING:
            _LOGGER.info("Charging stopped — force update queued, hourly loop cancelled")
            self._stop_hourly_force_update()
            if not self._active_car_is_guest and self.car is not None:
                self.hass.async_create_task(self.car.async_force_update())

    # ------------------------------------------------------------------
    # Plug-in / charge complete
    # ------------------------------------------------------------------

    async def _async_handle_plugin(self) -> None:
        _LOGGER.info("Car plugged in — requesting UVO update, rebuilding schedule in %ss", PLUGIN_DELAY_S)
        if not self._active_car_is_guest and self.car is not None:
            await self.car.async_force_update()

        @callback
        def _delayed_rebuild(_now: Any) -> None:
            self.hass.async_create_task(self._async_rebuild_schedule())

        async_call_later(self.hass, PLUGIN_DELAY_S, _delayed_rebuild)

        # Start transaction if none is active. _transaction_active is already
        # synced from the charger's trx key in _handle_mqtt_message.
        if not self._transaction_active:
            await self.charger.async_start_transaction(force_charge=False)
            self._transaction_active = True
        else:
            _LOGGER.info("Plugin: transaction already active — skipping trx=1")

    def _stop_amp_adjust(self) -> None:
        if self._amp_adjust_task and not self._amp_adjust_task.done():
            self._amp_adjust_task.cancel()
            self._amp_adjust_task = None

    def _stop_hourly_force_update(self) -> None:
        if self._hourly_update_task and not self._hourly_update_task.done():
            _LOGGER.debug("Stopping hourly force-update loop")
            self._hourly_update_task.cancel()
        self._hourly_update_task = None

    def _start_hourly_force_update(self) -> None:
        _LOGGER.debug("Starting hourly force-update loop (long charging block detected)")
        self._stop_hourly_force_update()
        self._hourly_update_task = self.hass.async_create_task(
            self._async_hourly_force_update_loop()
        )

    async def _async_hourly_force_update_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(3600)
                if not self._active_car_is_guest and self.car is not None:
                    _LOGGER.debug("Hourly force update during long charging block")
                    await self.car.async_force_update()
        except asyncio.CancelledError:
            pass

    def _is_long_charging_block(self) -> bool:
        """Return True if the current/upcoming charging block exceeds 90 minutes."""
        now = dt_util.now()
        # Scheduled: count contiguous selected slots from now forward
        if self.schedule:
            upcoming = sorted(
                [s for s in self.schedule if s["selected"] and s["end"] > now],
                key=lambda s: s["start"],
            )
            if upcoming:
                block_end = upcoming[0]["end"]
                for s in upcoming[1:]:
                    if s["start"] <= block_end:
                        block_end = max(block_end, s["end"])
                    else:
                        break
                return (block_end - max(now, upcoming[0]["start"])).total_seconds() > 5400
        # Opportunistic: count consecutive cheap slots remaining
        if self._current_price_data and self._cheap_threshold > 0:
            future = sorted(
                [
                    e for e in self._current_price_data
                    if dt_util.parse_datetime(e["end"]) > now
                    and e["price"] <= self._cheap_threshold
                ],
                key=lambda e: e["start"],
            )
            if future:
                block_end = dt_util.parse_datetime(future[0]["end"])
                for e in future[1:]:
                    if dt_util.parse_datetime(e["start"]) <= block_end:
                        block_end = max(block_end, dt_util.parse_datetime(e["end"]))
                    else:
                        break
                return (block_end - now).total_seconds() > 5400
        return False

    # ------------------------------------------------------------------
    # State-change callbacks
    # ------------------------------------------------------------------

    @callback
    def _handle_state_change(self, event: Any) -> None:
        self.hass.async_create_task(self._async_rebuild_schedule())

    @callback
    def _handle_charge_now_change(self, event: Any) -> None:
        new_state = event.data.get("new_state")
        if new_state:
            self._charge_now = new_state.state == "on"
        self.hass.async_create_task(self._async_apply_charger_command())

    @callback
    def _handle_threshold_change(self, event: Any) -> None:
        new_state = event.data.get("new_state")
        if new_state and new_state.state not in ("unknown", "unavailable"):
            try:
                self._cheap_threshold = float(new_state.state)
            except ValueError:
                pass
        self.hass.async_create_task(self._async_apply_charger_command())

    @callback
    def _handle_spread_threshold_change(self, event: Any) -> None:
        new_state = event.data.get("new_state")
        if new_state and new_state.state not in ("unknown", "unavailable"):
            try:
                self._price_spread_threshold = float(new_state.state)
            except ValueError:
                pass
        self.hass.async_create_task(self._async_rebuild_schedule())

    @callback
    def _handle_opportunistic_soc_limit_change(self, event: Any) -> None:
        new_state = event.data.get("new_state")
        if new_state and new_state.state not in ("unknown", "unavailable"):
            try:
                self._opportunistic_soc_limit = float(new_state.state)
            except ValueError:
                pass
        self.hass.async_create_task(self._async_apply_charger_command())

    @callback
    def _handle_charge_now_soc_limit_change(self, event: Any) -> None:
        new_state = event.data.get("new_state")
        if new_state and new_state.state not in ("unknown", "unavailable"):
            try:
                self._charge_now_soc_limit = float(new_state.state)
            except ValueError:
                pass
        self.hass.async_create_task(self._async_apply_charger_command())

    # ------------------------------------------------------------------
    # Schedule building
    # ------------------------------------------------------------------

    def _sync_settings_from_ha(self) -> None:
        """Populate coordinator state from HA persisted states.

        Called at the start of every schedule rebuild. On startup this is the
        only reliable way to read entity state before async_added_to_hass()
        tasks have completed. After startup it is a no-op (same values).
        """
        from datetime import time as _time

        for day in WEEKDAYS:
            state = self.hass.states.get(self._entity_id("number", f"{day}_target_soc"))
            if state and state.state not in ("unknown", "unavailable"):
                try:
                    self._day_settings[day]["target_soc"] = int(float(state.state))
                except ValueError:
                    pass

            state = self.hass.states.get(self._entity_id("number", f"{day}_manual_kwh"))
            if state and state.state not in ("unknown", "unavailable"):
                try:
                    self._day_settings[day]["manual_kwh"] = float(state.state)
                except ValueError:
                    pass

            state = self.hass.states.get(self._entity_id("time", f"{day}_departure"))
            if state and state.state not in ("unknown", "unavailable", ""):
                try:
                    parts = [int(x) for x in state.state.split(":")]
                    self._day_settings[day]["departure"] = _time(
                        parts[0], parts[1], parts[2] if len(parts) > 2 else 0
                    )
                except (ValueError, IndexError):
                    pass

        state = self.hass.states.get(self._entity_id("switch", "smart_enabled"))
        if state and state.state not in ("unknown", "unavailable"):
            self._smart_enabled = state.state == "on"

    async def _async_rebuild_schedule(self, *_: Any) -> None:
        """Rebuild the charging schedule from current prices and settings."""
        self._sync_settings_from_ha()
        _LOGGER.debug(
            "Rebuild schedule: smart_enabled=%s, days=%s",
            self._smart_enabled,
            {d: self._day_settings[d] for d in WEEKDAYS if self._day_settings[d]["target_soc"] > 0 or self._day_settings[d]["manual_kwh"] > 0},
        )
        _LOGGER.debug(
            "Schedule rebuild: car_state=%s soc=%s target=%s departure=%s",
            self.car_state,
            self._last_current_soc,
            self._last_target_soc,
            self._next_departure_dt,
        )
        if not self._smart_enabled:
            self._schedule_status_reason = "Smart charging disabled"
            self.schedule = []
            self._update_schedule_sensors()
            if self._slot_timer_cancel:
                self._slot_timer_cancel()
                self._slot_timer_cancel = None
            if self._transaction_active:
                await self.charger.async_set_frc(1)
            return

        result = self._find_next_departure()
        if result is None:
            _LOGGER.info("No enabled departure found — clearing schedule")
            self._schedule_status_reason = "No departure configured for any day"
            self.schedule = []
            self._update_schedule_sensors()
            if self._slot_timer_cancel:
                self._slot_timer_cancel()
                self._slot_timer_cancel = None
            return

        departure_dt, target_soc, day_name = result
        self._next_departure_dt = departure_dt
        self._last_target_soc = int(target_soc)

        # Fetch prices
        today_str = dt_util.now().date().isoformat()
        tomorrow_str = (dt_util.now().date() + timedelta(days=1)).isoformat()

        @callback
        def _schedule_retry(_now: Any) -> None:
            self.hass.async_create_task(self._async_rebuild_schedule())

        today_prices = await self._async_fetch_nordpool_prices(today_str)
        if not today_prices:
            _LOGGER.warning(
                "Today's Nordpool prices unavailable — Nordpool may still be starting up, retrying in 5 min"
            )
            self._schedule_status_reason = "Waiting for Nordpool prices (retry in 5 min)"
            self._update_schedule_sensors()
            if self._tomorrow_retry_cancel:
                self._tomorrow_retry_cancel()
            self._tomorrow_retry_cancel = async_call_later(self.hass, 300, _schedule_retry)
            return
        # Nordpool returns prices in milli-SEK; convert to SEK/kWh
        today_prices = [{**p, "price": p["price"] / 1000} for p in today_prices]
        self._current_price_data = today_prices
        tomorrow_prices: list[dict] = []

        after_midnight = departure_dt.date() > dt_util.now().date()
        if after_midnight:
            tomorrow_prices = await self._async_fetch_nordpool_prices(tomorrow_str)
            if not tomorrow_prices:
                now = dt_util.now()
                retry_today = now.replace(
                    hour=NORDPOOL_PRICES_AVAILABLE_HOUR,
                    minute=NORDPOOL_PRICES_AVAILABLE_MINUTE,
                    second=0,
                    microsecond=0,
                )
                if now < retry_today:
                    delay = (retry_today - now).total_seconds()
                    _LOGGER.info(
                        "Tomorrow Nordpool prices not yet available — retrying at 13:30 (%.0fs)",
                        delay,
                    )
                    self._schedule_status_reason = "Waiting for tomorrow's prices (retry at 13:30)"
                    self._update_schedule_sensors()
                    if self._tomorrow_retry_cancel:
                        self._tomorrow_retry_cancel()
                    self._tomorrow_retry_cancel = async_call_later(self.hass, delay, _schedule_retry)
                    await self._async_apply_charger_command()
                    return
                # After 13:30 but prices still empty — Nordpool likely hasn't
                # finished loading yet (common on HA restart). Retry in 5 min.
                _LOGGER.warning(
                    "Tomorrow prices unavailable after 13:30 — "
                    "Nordpool may still be starting up, retrying in 5 min"
                )
                self._schedule_status_reason = "Tomorrow's prices unavailable after 13:30 — retry in 5 min"
                self._update_schedule_sensors()
                if self._tomorrow_retry_cancel:
                    self._tomorrow_retry_cancel()
                self._tomorrow_retry_cancel = async_call_later(self.hass, 300, _schedule_retry)
                await self._async_apply_charger_command()
                return
            tomorrow_prices = [{**p, "price": p["price"] / 1000} for p in tomorrow_prices]

        all_prices = today_prices + tomorrow_prices
        now = dt_util.now()

        # Only consider slots in the 24 hours immediately before departure,
        # so a far-future departure (e.g. 5 days away) doesn't pull in today's prices.
        window_start = max(now, departure_dt - timedelta(days=1))

        try:
            slots = [
                {
                    "start": dt_util.parse_datetime(s["start"]),
                    "end": dt_util.parse_datetime(s["end"]),
                    "price": s["price"],
                    "selected": False,
                }
                for s in all_prices
                if dt_util.parse_datetime(s["end"]) > now
                and dt_util.parse_datetime(s["start"]) >= window_start
                and dt_util.parse_datetime(s["start"]) < departure_dt
            ]
        except (KeyError, TypeError) as err:
            _LOGGER.error(
                "Failed to parse Nordpool price entries: %s. First entry: %s",
                err,
                all_prices[0] if all_prices else "empty",
            )
            return

        if not slots:
            if window_start > now:
                # Departure is far away; the slot window hasn't opened yet.
                # Retry when we enter the scheduling window (the day before departure).
                delay = (window_start - now).total_seconds()
                _LOGGER.info(
                    "Departure is %s — slot window opens in %.0f hours, scheduling retry",
                    dt_util.as_local(departure_dt).strftime("%A %H:%M"),
                    delay / 3600,
                )
                self._schedule_status_reason = (
                    f"Waiting until {dt_util.as_local(window_start).strftime('%a %H:%M')} "
                    f"to schedule for {dt_util.as_local(departure_dt).strftime('%a %H:%M')} departure"
                )
                self._update_schedule_sensors()
                if self._tomorrow_retry_cancel:
                    self._tomorrow_retry_cancel()
                self._tomorrow_retry_cancel = async_call_later(self.hass, delay, _schedule_retry)
                await self._async_apply_charger_command()  # ensure charger is paused
                return
            _LOGGER.warning("No future price slots before departure — clearing schedule")
            dep_str = dt_util.as_local(departure_dt).strftime("%H:%M")
            self._schedule_status_reason = f"No future price slots before departure at {dep_str}"
            self.schedule = []
            self._update_schedule_sensors()
            if self._slot_timer_cancel:
                self._slot_timer_cancel()
                self._slot_timer_cancel = None
            return

        kwh_needed = self._get_kwh_needed(target_soc, day_name)
        self._last_kwh_needed = kwh_needed
        # Use conservative planning speed to avoid under-booking slots
        max_charge_kw = SCHEDULE_PLANNING_AMP * 0.23 * self._charger_n_phases

        if kwh_needed < 0.5:
            _LOGGER.info("No charging needed (%.1f kWh needed)", kwh_needed)
            if self._active_car_is_guest or self.car is None:
                self._schedule_status_reason = "No kWh needed (guest mode or no car)"
            elif self._last_current_soc is not None and self._last_current_soc >= target_soc:
                self._schedule_status_reason = (
                    f"Already at target ({self._last_current_soc:.0f}% ≥ {target_soc:.0f}%)"
                )
            else:
                self._schedule_status_reason = f"< 0.5 kWh needed ({kwh_needed:.2f} kWh)"
            self.schedule = []
            self._update_schedule_sensors()
            if self._slot_timer_cancel:
                self._slot_timer_cancel()
                self._slot_timer_cancel = None
            await self._async_apply_charger_command()
            return

        all_slot_prices = [s["price"] for s in slots]
        price_spread = max(all_slot_prices) - min(all_slot_prices)

        if price_spread < self._price_spread_threshold:
            _LOGGER.info(
                "Price spread %.3f < threshold %.3f — selecting all slots (continuous charging)",
                price_spread,
                self._price_spread_threshold,
            )
            for s in slots:
                s["selected"] = True
            self._schedule_status_reason = (
                f"All {len(slots)} slots selected (price spread below threshold)"
            )
        else:
            n_slots = math.ceil(kwh_needed / (max_charge_kw * 0.25))
            _select_slots(slots, n_slots, self._price_spread_threshold)
            n_selected = sum(1 for s in slots if s["selected"])
            self._schedule_status_reason = (
                f"{n_selected} slots selected (cheapest of {len(slots)} available)"
            )

        self.schedule = slots

        self._update_schedule_sensors()
        await self._async_apply_charger_command()
        self._schedule_next_slot_timer()

        selected = [s for s in slots if s["selected"]]
        if selected:
            avg_price = sum(s["price"] for s in selected) / len(selected)
            est_cost = kwh_needed * avg_price
            next_slot = min(s["start"] for s in selected)
            _LOGGER.info(
                "Schedule: %.1f kWh, %d slots (%.1fh) selected, avg %.2f SEK/kWh, est %.2f SEK, next: %s, dep: %s (%s)",
                kwh_needed,
                len(selected),
                len(selected) * 0.25,
                avg_price,
                est_cost,
                dt_util.as_local(next_slot).strftime("%H:%M"),
                day_name,
                departure_dt.strftime("%H:%M"),
            )

    def _get_kwh_needed(self, target_soc: float, day_name: str) -> float:
        """Return kWh needed: manual override if set, else SoC-based calculation."""
        manual_kwh = self._day_settings[day_name]["manual_kwh"]
        if manual_kwh > 0:
            return manual_kwh
        if self._active_car_is_guest:
            _LOGGER.warning(
                "Guest mode active but manual kWh is 0 for %s — no charging scheduled", day_name
            )
            return 0.0
        if self.car is None:
            _LOGGER.warning(
                "No car selected but manual kWh is 0 for %s — no charging scheduled", day_name
            )
            return 0.0
        current_soc = self.car.get_soc()
        self._last_current_soc = current_soc
        return max(0.0, (target_soc - current_soc) / 100 * self._battery_capacity / self._efficiency)

    def _find_next_departure(
        self,
    ) -> tuple[datetime, float, str] | None:
        """Return (departure_datetime, target_soc, day_name) for the next active day."""
        now = dt_util.now()
        for offset in range(7):
            candidate = now + timedelta(days=offset)
            day_name = WEEKDAYS[candidate.weekday()]
            day = self._day_settings[day_name]
            if day["target_soc"] <= 0 and day["manual_kwh"] <= 0:
                continue
            dep_time: time | None = day["departure"]
            if dep_time is None:
                continue
            departure_dt = dt_util.as_local(
                datetime.combine(candidate.date(), dep_time).replace(
                    tzinfo=dt_util.DEFAULT_TIME_ZONE
                )
            )
            if departure_dt > now:
                return departure_dt, float(day["target_soc"]), day_name
        return None

    async def _async_fetch_nordpool_prices(self, date_str: str) -> list[dict]:
        """Call nordpool.get_price_indices_for_date and return the price list."""
        nordpool_entries = self.hass.config_entries.async_entries("nordpool")
        if not nordpool_entries:
            _LOGGER.warning("No Nordpool config entry found — cannot fetch prices")
            return []
        try:
            result = await self.hass.services.async_call(
                "nordpool",
                "get_price_indices_for_date",
                {"date": date_str, "config_entry": nordpool_entries[0].entry_id, "resolution": 15},
                blocking=True,
                return_response=True,
            )
            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                # Try known keys first
                for key in ("prices", "entries", "data", "result"):
                    if key in result and isinstance(result[key], list):
                        return result[key]
                # Fallback: return the first non-empty list value found
                for val in result.values():
                    if isinstance(val, list) and val:
                        return val
                _LOGGER.info(
                    "Nordpool response for %s has unexpected format — keys: %s, value: %s",
                    date_str,
                    list(result.keys()),
                    result,
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Nordpool fetch for %s failed: %s", date_str, err)
        return []

    def _in_selected_slot(self) -> bool:
        now = dt_util.now()
        return any(
            s["selected"] and s["start"] <= now < s["end"]
            for s in self.schedule
        )

    def _is_current_slot_cheap(self) -> bool:
        """Return True if current slot is ≤ threshold and a ≥1h consecutive cheap block follows."""
        if self._cheap_threshold <= 0:
            return False
        now = dt_util.now()
        sorted_prices = sorted(
            self._current_price_data,
            key=lambda e: dt_util.parse_datetime(e["start"]),
        )
        current_idx = None
        for i, entry in enumerate(sorted_prices):
            start = dt_util.parse_datetime(entry["start"])
            end = dt_util.parse_datetime(entry["end"])
            if start <= now < end:
                current_idx = i
                break
        if current_idx is None:
            return False
        consecutive_slots = 0
        for entry in sorted_prices[current_idx:]:
            if entry["price"] <= self._cheap_threshold:
                consecutive_slots += 1
            else:
                break
        return consecutive_slots >= 4

    # ------------------------------------------------------------------
    # Charger control
    # ------------------------------------------------------------------

    async def _async_apply_charger_command(self) -> None:
        """Send frc=2 (charge) or frc=1 (pause) based on schedule and overrides."""
        cheap_slot = self._is_current_slot_cheap()
        now_price: float | None = None
        if cheap_slot and not self._in_selected_slot() and not self._charge_now:
            now_price = next(
                (
                    e["price"]
                    for e in self._current_price_data
                    if dt_util.parse_datetime(e["start"]) <= dt_util.now() < dt_util.parse_datetime(e["end"])
                ),
                None,
            )
            if now_price is not None:
                _LOGGER.info(
                    "Cheap price threshold triggered: %.2f ≤ %.2f SEK/kWh",
                    now_price,
                    self._cheap_threshold,
                )
        in_slot = self._in_selected_slot()

        # No cable connected — nothing to do
        if self.car_state == CAR_IDLE:
            _LOGGER.debug("_async_apply_charger_command: car idle (no cable) — skipping")
            return

        should_charge = self._charge_now or in_slot or cheap_slot

        # Update status reason to reflect current charging action
        if self._charge_now:
            self._schedule_status_reason = "Manual override active — charging regardless of price"
        elif in_slot:
            now = dt_util.now()
            slot = next(
                (s for s in self.schedule if s["selected"] and s["start"] <= now < s["end"]),
                None,
            )
            if slot:
                s_start = dt_util.as_local(slot["start"]).strftime("%H:%M")
                s_end = dt_util.as_local(slot["end"]).strftime("%H:%M")
                self._schedule_status_reason = f"In selected slot ({s_start}–{s_end})"
            else:
                self._schedule_status_reason = "In selected slot"
        elif cheap_slot:
            price_str = f"{now_price:.2f}" if now_price is not None else "?"
            self._schedule_status_reason = (
                f"Cheap price ({price_str} SEK/kWh ≤ {self._cheap_threshold:.2f} threshold)"
            )
        elif not self.schedule or not any(s["selected"] for s in self.schedule):
            pass  # reason already set by rebuild (e.g. "Smart charging disabled")
        else:
            next_time = self.get_next_slot_time()
            next_str = dt_util.as_local(next_time).strftime("%H:%M") if next_time else "none"
            self._schedule_status_reason = f"Paused between slots (next: {next_str})"

        _LOGGER.debug(
            "Apply charger command: car_state=%s transaction_active=%s "
            "should_charge=%s (charge_now=%s in_slot=%s cheap=%s)",
            self.car_state,
            self._transaction_active,
            should_charge,
            self._charge_now,
            in_slot,
            cheap_slot,
        )

        if should_charge:
            if self._charge_now:
                desired_limit = int(self._charge_now_soc_limit)
            elif in_slot:
                desired_limit = int(self._last_target_soc)
            else:  # cheap_slot
                desired_limit = int(self._opportunistic_soc_limit)

            if self.car and not self._active_car_is_guest and desired_limit != self._last_sent_car_limit:
                _LOGGER.info(
                    "Car charge limit: %s → %d%% (mode: %s)",
                    self._last_sent_car_limit,
                    desired_limit,
                    "charge_now" if self._charge_now else ("in_slot" if in_slot else "cheap"),
                )
                await self.car.async_set_charge_limit(desired_limit)
                self._last_sent_car_limit = desired_limit

            if not self._transaction_active:
                # No session running yet — start one
                _LOGGER.info("Starting new transaction (trx=1 + frc=2, car_state=%s)", self.car_state)
                await self.charger.async_start_transaction(force_charge=True)
                self._transaction_active = True
            else:
                _LOGGER.debug("Resuming charge: frc=2 (car_state=%s)", self.car_state)
                await self.charger.async_set_frc(2)
        else:
            if self._transaction_active:
                _LOGGER.debug("Pausing charge: frc=1 (car_state=%s)", self.car_state)
                await self.charger.async_set_frc(1)

        self._update_schedule_sensors()

    # ------------------------------------------------------------------
    # Amp adjustment loop
    # ------------------------------------------------------------------

    async def _async_amp_adjust_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(AMP_ADJUST_INTERVAL_S)
                await self._async_do_amp_adjust()
        except asyncio.CancelledError:
            pass

    async def _async_do_amp_adjust(self) -> None:
        if self.car_state != CAR_CHARGING:
            return

        phases: list[float] = []
        for entity_id in self._phase_entities:
            state = self.hass.states.get(entity_id)
            if state is None or state.state in ("unknown", "unavailable"):
                _LOGGER.debug("Phase sensor %s unavailable — skipping amp adjust", entity_id)
                return
            try:
                phases.append(float(state.state))
            except ValueError:
                return

        # Remove charger contribution so we see household baseline.
        # 3-phase charger draws last_sent_amp on all three phases simultaneously.
        if self._charger_n_phases == 3:
            phases = [max(0.0, p - self._last_sent_amp) for p in phases]
        else:
            charger_idx = self._charger_phase - 1
            phases[charger_idx] = max(0.0, phases[charger_idx] - self._last_sent_amp)

        headroom = min(self._breaker_limit - p for p in phases)
        new_amp = int(max(self._min_amp, min(self._max_amp, round(headroom))))

        _LOGGER.debug(
            "Amp adjust eval: phases=%s headroom=%.1f → new=%dA last=%dA (delta=%d)",
            [round(p, 1) for p in phases],
            headroom,
            new_amp,
            self._last_sent_amp,
            abs(new_amp - self._last_sent_amp),
        )

        if abs(new_amp - self._last_sent_amp) < 1:
            return

        _LOGGER.debug(
            "Amp adjust: L1=%.1f L2=%.1f L3=%.1f headroom=%.1f → %dA",
            *phases,
            headroom,
            new_amp,
        )
        self._last_sent_amp = new_amp
        await self.charger.async_set_amp(new_amp)

    # ------------------------------------------------------------------
    # Sensor push helpers
    # ------------------------------------------------------------------

    def _update_schedule_sensors(self) -> None:
        if self._schedule_sensor:
            self._schedule_sensor.async_write_ha_state()
        if self._next_slot_sensor:
            self._next_slot_sensor.async_write_ha_state()

    def get_schedule_summary(self) -> str:
        selected = [s for s in self.schedule if s["selected"]]
        if not selected:
            return self._schedule_status_reason

        avg_price = sum(s["price"] for s in selected) / len(selected)
        prefix = f"{len(selected)} slots | {avg_price:.2f} SEK avg"

        if self._charge_now:
            return f"{prefix} | override"
        if self._in_selected_slot():
            now = dt_util.now()
            slot = next(
                (s for s in selected if s["start"] <= now < s["end"]),
                None,
            )
            if slot:
                s_start = dt_util.as_local(slot["start"]).strftime("%H:%M")
                s_end = dt_util.as_local(slot["end"]).strftime("%H:%M")
                return f"{prefix} | charging ({s_start}–{s_end})"
        if self._is_current_slot_cheap():
            return f"{prefix} | cheap price"
        next_slot = self.get_next_slot_time()
        if next_slot:
            return f"{prefix} | paused (next: {dt_util.as_local(next_slot).strftime('%H:%M')})"
        return f"{prefix} | all slots done"

    def get_schedule_debug_attrs(self) -> dict:
        """Return rich debug attributes for the schedule sensor."""
        car_state_names = {1: "idle", 2: "charging", 3: "connected", 4: "complete"}
        selected = [s for s in self.schedule if s["selected"]]
        return {
            "status_reason": self._schedule_status_reason,
            "kwh_needed": round(self._last_kwh_needed, 2),
            "current_soc": self._last_current_soc,
            "target_soc": self._last_target_soc,
            "departure": (
                dt_util.as_local(self._next_departure_dt).strftime("%a %H:%M")
                if self._next_departure_dt
                else None
            ),
            "charger_state": car_state_names.get(self.car_state, str(self.car_state)),
            "in_selected_slot": self._in_selected_slot(),
            "charge_now_active": self._charge_now,
            "cheap_price_active": self._is_current_slot_cheap(),
            "slots": [
                {
                    "start": s["start"].isoformat(),
                    "end": s["end"].isoformat(),
                    "price": round(s["price"], 4),
                }
                for s in sorted(selected, key=lambda s: s["start"])
            ],
        }

    def get_next_slot_time(self) -> datetime | None:
        now = dt_util.now()
        future_selected = [s for s in self.schedule if s["selected"] and s["start"] >= now]
        if not future_selected:
            return None
        return min(s["start"] for s in future_selected)

    # ------------------------------------------------------------------
    # Per-day settings (called by entity platform files)
    # ------------------------------------------------------------------

    def set_day_departure(self, day: str, departure: time | None) -> None:
        self._day_settings[day]["departure"] = departure

    def set_day_target_soc(self, day: str, target_soc: float) -> None:
        self._day_settings[day]["target_soc"] = int(target_soc)

    def set_day_manual_kwh(self, day: str, value: float) -> None:
        self._day_settings[day]["manual_kwh"] = value

    def get_day_manual_kwh(self, day: str) -> float:
        return self._day_settings[day]["manual_kwh"]

    def get_day_departure(self, day: str) -> time | None:
        return self._day_settings[day]["departure"]

    def get_day_target_soc(self, day: str) -> int:
        return self._day_settings[day]["target_soc"]

    def set_smart_enabled(self, enabled: bool) -> None:
        self._smart_enabled = enabled

    def get_smart_enabled(self) -> bool:
        return self._smart_enabled

    def set_charge_now(self, enabled: bool) -> None:
        self._charge_now = enabled

    def get_charge_now(self) -> bool:
        return self._charge_now

    def set_cheap_threshold(self, value: float) -> None:
        self._cheap_threshold = value

    def get_cheap_threshold(self) -> float:
        return self._cheap_threshold

    def set_price_spread_threshold(self, value: float) -> None:
        self._price_spread_threshold = value

    def get_price_spread_threshold(self) -> float:
        return self._price_spread_threshold

    def get_opportunistic_soc_limit(self) -> float:
        return self._opportunistic_soc_limit

    def set_opportunistic_soc_limit(self, v: float) -> None:
        self._opportunistic_soc_limit = float(v)

    def get_charge_now_soc_limit(self) -> float:
        return self._charge_now_soc_limit

    def set_charge_now_soc_limit(self, v: float) -> None:
        self._charge_now_soc_limit = float(v)
