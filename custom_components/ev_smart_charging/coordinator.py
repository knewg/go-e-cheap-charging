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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .car.kia_uvo import KiaUvoDriver
from .charger.goe import GoeCharger
from .const import (
    AMP_ADJUST_INTERVAL_S,
    CAR_CHARGING,
    CAR_COMPLETE,
    CAR_CONNECTED,
    CAR_IDLE,
    CONF_BATTERY_CAPACITY,
    CONF_BREAKER_LIMIT,
    CONF_CAR_DEVICE_ID,
    CONF_CAR_SOC_ENTITY,
    CONF_CHARGER_PHASE,
    CONF_CHARGER_SERIAL,
    CONF_EFFICIENCY,
    CONF_MAX_AMP,
    CONF_MIN_AMP,
    CONF_PHASE_L1_ENTITY,
    CONF_PHASE_L2_ENTITY,
    CONF_PHASE_L3_ENTITY,
    DEFAULT_CHEAP_THRESHOLD,
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


class EvSmartChargingCoordinator(DataUpdateCoordinator):
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
        self._min_amp: int = cfg[CONF_MIN_AMP]
        self._max_amp: int = cfg[CONF_MAX_AMP]

        self._phase_entities: list[str] = [
            cfg[CONF_PHASE_L1_ENTITY],
            cfg[CONF_PHASE_L2_ENTITY],
            cfg[CONF_PHASE_L3_ENTITY],
        ]

        self.charger = GoeCharger(hass, self._serial)
        self.car = KiaUvoDriver(hass, cfg[CONF_CAR_SOC_ENTITY], cfg[CONF_CAR_DEVICE_ID])

        # Runtime state
        self.car_state: int = CAR_IDLE
        self.schedule: list[dict] = []
        self._transaction_active: bool = False
        self._last_sent_amp: int = 0
        self._smart_enabled: bool = False
        self._charge_now: bool = False
        self._cheap_threshold: float = DEFAULT_CHEAP_THRESHOLD
        self._price_spread_threshold: float = DEFAULT_PRICE_SPREAD_THRESHOLD
        self._current_price_data: list[dict] = []

        # Per-day settings: {day: {"enabled": bool, "departure": time|None, "target_soc": int}}
        self._day_settings: dict[str, dict] = {
            day: {"enabled": False, "departure": None, "target_soc": DEFAULT_TARGET_SOC}
            for day in WEEKDAYS
        }

        # Cancellation handles
        self._mqtt_unsub: Any = None
        self._state_unsubs: list[Any] = []
        self._amp_adjust_task: asyncio.Task | None = None
        self._tomorrow_retry_cancel: Any = None

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

        # Entities that should trigger a schedule rebuild
        entities_to_watch: list[str] = [self.car.soc_entity_id]
        for day in WEEKDAYS:
            entities_to_watch.extend(
                [
                    self._entity_id("switch", f"{day}_enabled"),
                    self._entity_id("time", f"{day}_departure"),
                    self._entity_id("number", f"{day}_target_soc"),
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

        await self._async_rebuild_schedule()

    async def async_shutdown(self) -> None:
        """Unsubscribe everything and cancel tasks."""
        if self._mqtt_unsub:
            self._mqtt_unsub()
        for unsub in self._state_unsubs:
            unsub()
        self._state_unsubs.clear()
        if self._amp_adjust_task and not self._amp_adjust_task.done():
            self._amp_adjust_task.cancel()
        if self._tomorrow_retry_cancel:
            self._tomorrow_retry_cancel()

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
        status = GoeCharger.parse_status(msg.payload)
        if not status:
            return

        new_car_state = status.get("car")
        if new_car_state is None:
            return
        new_car_state = int(new_car_state)

        prev = self.car_state
        self.car_state = new_car_state

        _LOGGER.debug("go-e car state: %s → %s", prev, new_car_state)

        # Plug-in: idle → connected/waiting
        if prev == CAR_IDLE and new_car_state == CAR_CONNECTED:
            self.hass.async_create_task(self._async_handle_plugin())

        # Charge complete
        elif new_car_state == CAR_COMPLETE and prev != CAR_COMPLETE:
            self._transaction_active = False
            self._stop_amp_adjust()
            _LOGGER.info("Charge complete")

        # Start/stop amp-adjust loop based on charging state
        if new_car_state == CAR_CHARGING and (
            self._amp_adjust_task is None or self._amp_adjust_task.done()
        ):
            self._amp_adjust_task = self.hass.async_create_task(
                self._async_amp_adjust_loop()
            )
        elif new_car_state != CAR_CHARGING:
            self._stop_amp_adjust()

    # ------------------------------------------------------------------
    # Plug-in / charge complete
    # ------------------------------------------------------------------

    async def _async_handle_plugin(self) -> None:
        _LOGGER.info("Car plugged in — requesting UVO update, rebuilding schedule in %ss", PLUGIN_DELAY_S)
        await self.car.async_force_update()

        @callback
        def _delayed_rebuild(_now: Any) -> None:
            self.hass.async_create_task(self._async_rebuild_schedule())

        async_call_later(self.hass, PLUGIN_DELAY_S, _delayed_rebuild)

        # Start transaction immediately so car knows we intend to charge.
        # Use frc=1 (paused) until schedule is built.
        if not self._transaction_active:
            await self.charger.async_start_transaction(force_charge=False)
            self._transaction_active = True

    def _stop_amp_adjust(self) -> None:
        if self._amp_adjust_task and not self._amp_adjust_task.done():
            self._amp_adjust_task.cancel()
            self._amp_adjust_task = None

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

    # ------------------------------------------------------------------
    # Schedule building
    # ------------------------------------------------------------------

    async def _async_rebuild_schedule(self, *_: Any) -> None:
        """Rebuild the charging schedule from current prices and settings."""
        if not self._smart_enabled:
            self.schedule = []
            self._update_schedule_sensors()
            if self._transaction_active:
                await self.charger.async_set_frc(1)
            return

        current_soc = self.car.get_soc()

        result = self._find_next_departure()
        if result is None:
            _LOGGER.debug("No enabled departure found — clearing schedule")
            self.schedule = []
            self._update_schedule_sensors()
            return

        departure_dt, target_soc, day_name = result

        # Fetch prices
        today_str = dt_util.now().date().isoformat()
        tomorrow_str = (dt_util.now().date() + timedelta(days=1)).isoformat()

        today_prices = await self._async_fetch_nordpool_prices(today_str)
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
                    if self._tomorrow_retry_cancel:
                        self._tomorrow_retry_cancel()
                    self._tomorrow_retry_cancel = async_call_later(
                        self.hass,
                        delay,
                        lambda _now: self.hass.async_create_task(
                            self._async_rebuild_schedule()
                        ),
                    )
                    await self._async_apply_charger_command()
                    return
                _LOGGER.warning(
                    "Tomorrow prices still unavailable after 13:30 — "
                    "cannot schedule overnight charge"
                )
                await self._async_apply_charger_command()
                return

        all_prices = today_prices + tomorrow_prices
        now = dt_util.now()

        slots = [
            {
                "start": dt_util.parse_datetime(s["start"]),
                "end": dt_util.parse_datetime(s["end"]),
                "price": s["value"],
                "selected": False,
            }
            for s in all_prices
            if dt_util.parse_datetime(s["end"]) > now
            and dt_util.parse_datetime(s["start"]) < departure_dt
        ]

        if not slots:
            _LOGGER.warning("No future price slots before departure — clearing schedule")
            self.schedule = []
            self._update_schedule_sensors()
            return

        kwh_needed = max(
            0.0,
            (target_soc - current_soc) / 100 * self._battery_capacity / self._efficiency,
        )
        # Use conservative planning speed to avoid under-booking slots
        max_charge_kw = SCHEDULE_PLANNING_AMP * 0.23  # single-phase ~230 V

        if kwh_needed < 0.5:
            _LOGGER.info("Already near target SoC (%.0f%% → %.0f%%) — no charging needed", current_soc, target_soc)
            self.schedule = []
            self._update_schedule_sensors()
            await self._async_apply_charger_command()
            return

        # --- Group available slots into 1-hour buckets ---
        from collections import defaultdict
        hour_buckets: dict = defaultdict(list)
        for s in slots:
            hour_key = s["start"].replace(minute=0, second=0, microsecond=0)
            hour_buckets[hour_key].append(s)

        hours = sorted(hour_buckets.keys())
        hour_price = {
            h: sum(s["price"] for s in hour_buckets[h]) / len(hour_buckets[h])
            for h in hours
        }

        all_slot_prices = [hour_price[h] for h in hours]
        price_spread = max(all_slot_prices) - min(all_slot_prices)

        if price_spread < self._price_spread_threshold:
            _LOGGER.info(
                "Price spread %.3f < threshold %.3f — selecting all slots (continuous charging)",
                price_spread,
                self._price_spread_threshold,
            )
            selected_hours = set(hours)
        else:
            hours_needed = math.ceil(kwh_needed / max_charge_kw)
            actual_hours = min(hours_needed, len(hours))

            # Step 1: pick the cheapest N hours
            sorted_by_price = sorted(hours, key=lambda h: hour_price[h])
            selected_hours = set(sorted_by_price[:actual_hours])

            # Step 2: fill gaps between selected hours if gap price ≤ max_selected + threshold
            max_selected_price = max(hour_price[h] for h in selected_hours)
            changed = True
            while changed:
                changed = False
                selected_sorted = sorted(selected_hours)
                for i in range(len(selected_sorted) - 1):
                    h1 = selected_sorted[i]
                    h2 = selected_sorted[i + 1]
                    gap: list = []
                    cursor = h1 + timedelta(hours=1)
                    while cursor < h2:
                        if cursor in hour_price:
                            gap.append(cursor)
                        cursor += timedelta(hours=1)
                    if gap and all(
                        hour_price[g] <= max_selected_price + self._price_spread_threshold
                        for g in gap
                    ):
                        for g in gap:
                            selected_hours.add(g)
                        max_selected_price = max(hour_price[h] for h in selected_hours)
                        changed = True

        # Mark individual slots whose hour bucket was selected
        for s in slots:
            hour_key = s["start"].replace(minute=0, second=0, microsecond=0)
            if hour_key in selected_hours:
                s["selected"] = True

        self.schedule = slots

        # Set car charge limit to today's target SoC before charging starts
        await self.car.async_set_charge_limit(int(target_soc))

        self._update_schedule_sensors()
        await self._async_apply_charger_command()

        selected = [s for s in slots if s["selected"]]
        if selected:
            avg_price = sum(s["price"] for s in selected) / len(selected)
            est_cost = kwh_needed * avg_price
            next_slot = min(s["start"] for s in selected)
            _LOGGER.info(
                "Schedule: %.1f kWh, %d hours selected, avg %.2f SEK/kWh, est %.2f SEK, next: %s, dep: %s (%s)",
                kwh_needed,
                len(selected_hours),
                avg_price,
                est_cost,
                next_slot.strftime("%H:%M"),
                day_name,
                departure_dt.strftime("%H:%M"),
            )

    def _find_next_departure(
        self,
    ) -> tuple[datetime, float, str] | None:
        """Return (departure_datetime, target_soc, day_name) for the next enabled day."""
        now = dt_util.now()
        for offset in range(7):
            candidate = now + timedelta(days=offset)
            day_name = WEEKDAYS[candidate.weekday()]
            day = self._day_settings[day_name]
            if not day["enabled"]:
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
        try:
            result = await self.hass.services.async_call(
                "nordpool",
                "get_price_indices_for_date",
                {"date": date_str},
                blocking=True,
                return_response=True,
            )
            if isinstance(result, dict):
                # The response format may vary; try common keys
                for key in ("prices", "entries", "data"):
                    if key in result and isinstance(result[key], list):
                        return result[key]
                # If the result itself is a list-like
                if isinstance(result.get("result"), list):
                    return result["result"]
            if isinstance(result, list):
                return result
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
            if entry["value"] <= self._cheap_threshold:
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
        if cheap_slot and not self._in_selected_slot() and not self._charge_now:
            now_price = next(
                (
                    e["value"]
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
        should_charge = self._charge_now or self._in_selected_slot() or cheap_slot

        if should_charge:
            if not self._transaction_active:
                # No session running yet — start one
                await self.charger.async_start_transaction(force_charge=True)
                self._transaction_active = True
            else:
                await self.charger.async_set_frc(2)
        else:
            if self._transaction_active:
                await self.charger.async_set_frc(1)

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

        # Remove charger contribution from its phase so we see household baseline
        charger_idx = self._charger_phase - 1
        phases[charger_idx] = max(0.0, phases[charger_idx] - self._last_sent_amp)

        headroom = min(self._breaker_limit - p for p in phases)
        new_amp = int(max(self._min_amp, min(self._max_amp, round(headroom))))

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
            return "No schedule"
        kwh = self._battery_capacity  # approximate; exact value stored during build
        avg_price = sum(s["price"] for s in selected) / len(selected)
        next_slot = min(s["start"] for s in selected)
        return (
            f"{len(selected)} slots | avg {avg_price:.2f} SEK/kWh | "
            f"next: {next_slot.strftime('%H:%M')}"
        )

    def get_next_slot_time(self) -> datetime | None:
        now = dt_util.now()
        future_selected = [s for s in self.schedule if s["selected"] and s["start"] >= now]
        if not future_selected:
            return None
        return min(s["start"] for s in future_selected)

    # ------------------------------------------------------------------
    # Per-day settings (called by entity platform files)
    # ------------------------------------------------------------------

    def set_day_enabled(self, day: str, enabled: bool) -> None:
        self._day_settings[day]["enabled"] = enabled

    def set_day_departure(self, day: str, departure: time | None) -> None:
        self._day_settings[day]["departure"] = departure

    def set_day_target_soc(self, day: str, target_soc: float) -> None:
        self._day_settings[day]["target_soc"] = int(target_soc)

    def get_day_enabled(self, day: str) -> bool:
        return self._day_settings[day]["enabled"]

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
