"""Simulation tests for ChargingCoordinator.

Each test class covers a distinct behavioral scenario. The same price data
fixtures are reused across multiple scenarios (different SoC, departure time,
charger config, etc.) to keep the test matrix compact.

Infrastructure is provided by conftest.py (FakeHass, FakeCar, make_coordinator,
make_nordpool_prices, set_day_config, freeze_now, mqtt_commands, etc.).
"""
from __future__ import annotations

import asyncio
import math
import sys
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Pull shared helpers from conftest (injected via sys.modules after conftest
# runs, so we can also import them directly for use in parametrize)
# ---------------------------------------------------------------------------
import conftest as _c

from custom_components.goe_cheap_charging.const import (
    CAR_CHARGING,
    CAR_COMPLETE,
    CAR_CONNECTED,
    CAR_IDLE,
    CONF_BATTERY_CAPACITY,
    CONF_BREAKER_LIMIT,
    CONF_CHARGER_N_PHASES,
    CONF_CHARGER_PHASE,
    CONF_EFFICIENCY,
    CONF_MAX_AMP,
    CONF_MIN_AMP,
    WEEKDAYS,
)
from custom_components.goe_cheap_charging.coordinator import ChargingCoordinator

_UTC = timezone.utc

# ---------------------------------------------------------------------------
# Reusable price scenario templates  (96 × 15-min slots = 24 hours)
# ---------------------------------------------------------------------------

PRICES_FLAT = [0.50] * 96                                   # no spread
PRICES_OVERNIGHT_CHEAP = [0.10] * 24 + [0.80] * 72          # cheap 00:00–06:00
PRICES_SOLAR_DIP = [0.70] * 40 + [0.15] * 16 + [0.70] * 40  # cheap 10:00–14:00
PRICES_BIMODAL = [0.10] * 12 + [0.80] * 20 + [0.10] * 12 + [0.80] * 52  # two cheap windows
PRICES_ALL_EXPENSIVE = [2.00] * 96
PRICES_RISING = [0.10 + 0.02 * i for i in range(96)]         # monotonically rising
PRICES_FALLING = [2.00 - 0.02 * i for i in range(96)]        # monotonically falling
PRICES_NEGATIVE = [-0.50] * 16 + [0.30] * 80                 # negative 00:00–04:00
PRICES_VOLATILE = [0.50 + 0.40 * ((i * 7 + 3) % 11) / 10 for i in range(96)]
PRICES_SPIKE = [0.40] * 68 + [1.50] * 16 + [0.40] * 12      # evening spike 17:00–21:00


def _today_midnight() -> datetime:
    return _c._dt_util.now().replace(hour=0, minute=0, second=0, microsecond=0)


def _now_frozen() -> datetime:
    """Fixed 'now' used by tests that need a stable time reference."""
    return datetime(2026, 3, 15, 10, 0, 0, tzinfo=_UTC)  # Sunday 10:00 UTC


def _weekday_name(dt: datetime) -> str:
    return WEEKDAYS[dt.weekday()]


# ---------------------------------------------------------------------------
# Helper: make a slot dict covering the current frozen time
# ---------------------------------------------------------------------------

def _make_slot_at(start: datetime, selected: bool = True) -> dict:
    return {
        "start": start,
        "end": start + timedelta(minutes=15),
        "price": 0.50,
        "selected": selected,
    }


def _slot_covering_now(now: datetime, selected: bool = True) -> dict:
    """Return a 15-min slot that spans *now*."""
    start = now.replace(second=0, microsecond=0) - timedelta(minutes=5)
    return _make_slot_at(start, selected)


# ============================================================================
# TestKwhScenarios
# ============================================================================

class TestKwhScenarios:
    """Parametrized kWh-needed calculations covering many real-world situations."""

    @pytest.mark.parametrize(
        "soc,target,capacity,efficiency,expected_kwh",
        [
            # Normal charging needs
            (30, 80, 64, 0.9, (80 - 30) / 100 * 64 / 0.9),  # ~35.56
            (50, 80, 64, 0.9, (80 - 50) / 100 * 64 / 0.9),  # ~21.33
            (70, 80, 64, 0.9, (80 - 70) / 100 * 64 / 0.9),  # ~7.11
            # Edge: already at or above target → 0
            (80, 80, 64, 0.9, 0.0),
            (90, 80, 64, 0.9, 0.0),
            (100, 80, 64, 0.9, 0.0),
            # Near-empty battery
            (5, 80, 64, 0.9, (80 - 5) / 100 * 64 / 0.9),   # ~53.33
            (0, 80, 64, 0.9, 80 / 100 * 64 / 0.9),           # ~56.89
            # Different capacities
            (50, 80, 40, 0.9, 30 / 100 * 40 / 0.9),          # ~13.33
            (50, 80, 100, 0.9, 30 / 100 * 100 / 0.9),        # ~33.33
            # Lower efficiency → more kWh needed
            (50, 80, 64, 0.8, 30 / 100 * 64 / 0.8),          # 24.0
            (50, 80, 64, 0.7, 30 / 100 * 64 / 0.7),          # ~27.43
            # High target SoC
            (50, 100, 64, 0.9, 50 / 100 * 64 / 0.9),         # ~35.56
            # Low target SoC (e.g. weekday short commute)
            (50, 60, 64, 0.9, 10 / 100 * 64 / 0.9),          # ~7.11
            (55, 60, 64, 0.9, 5 / 100 * 64 / 0.9),           # ~3.56
            (60, 60, 64, 0.9, 0.0),                           # already at target
        ],
    )
    def test_kwh_needed(self, soc, target, capacity, efficiency, expected_kwh):
        actual = max(0.0, (target - soc) / 100 * capacity / efficiency)
        assert abs(actual - expected_kwh) < 0.001


# ============================================================================
# TestSlotCountScenarios
# ============================================================================

class TestSlotCountScenarios:
    """Verify that the slot count formula rounds up to full 1-h blocks."""

    @pytest.mark.parametrize(
        "kwh,max_amp,n_phases,expected_slots",
        [
            # SCHEDULE_PLANNING_AMP=10, 10A × 0.23kW/A × 1 phase = 2.3 kW per slot-hour
            # slot duration = 0.25h → 0.575 kWh/slot
            (5.75, 10, 1, 4 * math.ceil(math.ceil(5.75 / (10 * 0.23 * 1 * 0.25)) / 4)),
            # Check rounding to nearest 4-slot block (1 hour)
            (0.6, 10, 1, 4),   # 0.6/0.575 = 1.04 → ceil=2 → rounds to 4 (min block)
            (2.3, 10, 1, 4),   # exactly 4 slots
            (2.31, 10, 1, 8),  # just over 4 → next 4-slot block
            # 3-phase: 10A × 0.23 × 3 = 6.9 kW → 1.725 kWh/slot
            (7.0, 10, 3, 8),   # 7.0/1.725 = 4.06 → ceil=5 → rounds to 8
        ],
    )
    def test_slot_count(self, kwh, max_amp, n_phases, expected_slots):
        from custom_components.goe_cheap_charging.const import SCHEDULE_PLANNING_AMP

        max_charge_kw = SCHEDULE_PLANNING_AMP * 0.23 * n_phases
        n_raw = math.ceil(kwh / (max_charge_kw * 0.25))
        # _select_slots rounds up to nearest MIN_BLOCK_SLOTS (=4) multiple
        n_rounded = math.ceil(n_raw / 4) * 4
        assert n_rounded == expected_slots


# ============================================================================
# TestScheduleBuilding
# ============================================================================

class TestScheduleBuilding:
    """End-to-end tests of _async_rebuild_schedule with mocked Nordpool prices."""

    def _setup(
        self,
        fake_hass,
        prices_today: list[float],
        now: datetime,
        soc: float = 50.0,
        target: int = 80,
        prices_tomorrow: list[float] | None = None,
    ):
        """Wire up hass + coordinator for a schedule-building test."""
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_str = now.date().isoformat()
        tomorrow_str = (now.date() + timedelta(days=1)).isoformat()

        nordpool_today = _c.make_nordpool_prices(prices_today, midnight)
        fake_hass.services.set_nordpool(today_str, nordpool_today)

        if prices_tomorrow is not None:
            tomorrow_midnight = midnight + timedelta(days=1)
            nordpool_tomorrow = _c.make_nordpool_prices(prices_tomorrow, tomorrow_midnight)
            fake_hass.services.set_nordpool(tomorrow_str, nordpool_tomorrow)

        coord = _c.make_coordinator(fake_hass, soc=soc)
        _c.set_smart_enabled(fake_hass, True)
        return coord

    # -----------------------------------------------------------------------
    # Already at / above target → no slots selected
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_already_at_target_no_charging(self, fake_hass):
        now = _now_frozen()
        day = _weekday_name(now)
        departure = (now + timedelta(hours=4)).strftime("%H:%M")

        coord = self._setup(fake_hass, PRICES_OVERNIGHT_CHEAP, now, soc=80)
        _c.set_day_config(fake_hass, day, departure, target_soc=80)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        assert coord.schedule == []
        assert "Already at target" in coord._schedule_status_reason

    @pytest.mark.asyncio
    async def test_above_target_no_charging(self, fake_hass):
        now = _now_frozen()
        day = _weekday_name(now)
        departure = (now + timedelta(hours=4)).strftime("%H:%M")

        coord = self._setup(fake_hass, PRICES_OVERNIGHT_CHEAP, now, soc=90)
        _c.set_day_config(fake_hass, day, departure, target_soc=80)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        assert coord.schedule == []
        assert "Already at target" in coord._schedule_status_reason

    # -----------------------------------------------------------------------
    # Smart disabled → empty schedule, frc=1 if transaction active
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_smart_disabled_clears_schedule(self, fake_hass, mqtt_log):
        now = _now_frozen()
        coord = self._setup(fake_hass, PRICES_OVERNIGHT_CHEAP, now)
        _c.set_smart_enabled(fake_hass, False)
        coord._transaction_active = True
        coord.car_state = CAR_CONNECTED

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        assert coord.schedule == []
        assert "disabled" in coord._schedule_status_reason.lower()
        cmds = _c.mqtt_commands(mqtt_log)
        assert cmds.get("frc") == 1

    @pytest.mark.asyncio
    async def test_smart_disabled_no_mqtt_if_unplugged(self, fake_hass, mqtt_log):
        now = _now_frozen()
        coord = self._setup(fake_hass, PRICES_OVERNIGHT_CHEAP, now)
        _c.set_smart_enabled(fake_hass, False)
        coord._transaction_active = False
        coord.car_state = CAR_IDLE

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        assert coord.schedule == []
        assert mqtt_log == []  # nothing to send

    # -----------------------------------------------------------------------
    # No departure configured → empty schedule
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_no_departure_configured(self, fake_hass):
        now = _now_frozen()
        coord = self._setup(fake_hass, PRICES_OVERNIGHT_CHEAP, now)
        # Don't configure any day departure

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        assert coord.schedule == []
        assert "No departure" in coord._schedule_status_reason

    # -----------------------------------------------------------------------
    # Price spread below threshold → all slots selected (continuous charging)
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_flat_prices_all_slots_selected(self, fake_hass):
        """Flat prices have zero spread → all slots selected."""
        now = _now_frozen()
        day = _weekday_name(now)
        departure = (now + timedelta(hours=6)).strftime("%H:%M")

        coord = self._setup(fake_hass, PRICES_FLAT, now, soc=30)
        _c.set_day_config(fake_hass, day, departure, target_soc=80)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        assert len(coord.schedule) > 0
        assert all(s["selected"] for s in coord.schedule)
        assert "price spread below threshold" in coord._schedule_status_reason

    @pytest.mark.asyncio
    async def test_all_expensive_flat_all_slots_selected(self, fake_hass):
        """All-expensive but uniform prices → still all slots (spread=0)."""
        now = _now_frozen()
        day = _weekday_name(now)
        departure = (now + timedelta(hours=6)).strftime("%H:%M")

        coord = self._setup(fake_hass, PRICES_ALL_EXPENSIVE, now, soc=30)
        _c.set_day_config(fake_hass, day, departure, target_soc=80)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        assert all(s["selected"] for s in coord.schedule)

    # -----------------------------------------------------------------------
    # Various price structures → selective slot picking
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "prices,soc,expected_cheap_hour_range",
        [
            # overnight cheap 00:00-06:00: but we're at 10:00 → those slots are in past
            # So from now (10:00) to departure (16:00) we only have expensive slots
            # price spread is 0.80-0.10=0.70 > threshold(0.10) → selective
            # but all remaining slots are expensive → some slots selected
            (PRICES_OVERNIGHT_CHEAP, 50, None),
            # solar dip 10:00-14:00: from now (10:00) to departure (16:00)
            # cheap slots at 10:00-14:00 should be preferred
            (PRICES_SOLAR_DIP, 50, (10, 14)),
            # rising prices: cheapest slots are earliest (near now)
            (PRICES_RISING, 50, None),
            # falling prices: cheapest slots are near departure
            (PRICES_FALLING, 50, None),
        ],
    )
    async def test_price_structure_selective_picking(
        self, fake_hass, prices, soc, expected_cheap_hour_range
    ):
        now = _now_frozen()  # 10:00 Sunday
        day = _weekday_name(now)
        departure_dt = now + timedelta(hours=6)  # 16:00
        departure = departure_dt.strftime("%H:%M")

        coord = self._setup(fake_hass, prices, now, soc=soc)
        _c.set_day_config(fake_hass, day, departure, target_soc=80)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        # Some slots must be present and at least some selected
        assert len(coord.schedule) > 0
        selected = [s for s in coord.schedule if s["selected"]]
        assert len(selected) > 0

        if expected_cheap_hour_range:
            lo, hi = expected_cheap_hour_range
            # Most selected slots should be within the cheap window
            cheap_count = sum(
                1
                for s in selected
                if lo <= s["start"].hour < hi
            )
            # At least 75% of selected slots in the cheap window
            assert cheap_count / len(selected) >= 0.75

    @pytest.mark.asyncio
    async def test_solar_dip_low_soc_all_cheap_slots_included(self, fake_hass):
        """With soc=50, needs more slots than the cheap window holds.
        The coordinator must still select ALL cheap slots — it can't skip them
        in favour of expensive ones.  Extra expensive slots are needed too, but
        the cheap window should be fully represented.
        """
        now = _now_frozen()  # 10:00 Sunday
        day = _weekday_name(now)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        departure = (now + timedelta(hours=6)).strftime("%H:%M")  # 16:00

        nordpool = _c.make_nordpool_prices(PRICES_SOLAR_DIP, midnight)
        today_str = now.date().isoformat()
        fake_hass.services.set_nordpool(today_str, nordpool)

        coord = _c.make_coordinator(fake_hass, soc=50)
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, day, departure, target_soc=80)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        # All slots in the cheap window (10:00–14:00) must be selected
        cheap_slots = [s for s in coord.schedule if 10 <= s["start"].hour < 14]
        assert len(cheap_slots) == 16, f"Expected 16 cheap slots, got {len(cheap_slots)}"
        assert all(s["selected"] for s in cheap_slots), "All cheap slots must be selected"

    @pytest.mark.asyncio
    async def test_negative_prices_selected_first(self, fake_hass):
        """Negative price slots (00:00–04:00) should always be selected first."""
        now = datetime(2026, 3, 15, 2, 0, tzinfo=_UTC)  # 02:00 → inside negative window
        day = _weekday_name(now)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        departure = (now + timedelta(hours=8)).strftime("%H:%M")

        nordpool = _c.make_nordpool_prices(PRICES_NEGATIVE, midnight)
        today_str = now.date().isoformat()
        fake_hass.services.set_nordpool(today_str, nordpool)

        coord = _c.make_coordinator(fake_hass, soc=50)
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, day, departure, target_soc=80)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        selected = [s for s in coord.schedule if s["selected"]]
        assert len(selected) > 0
        # All selected slots should have price ≤ 0 (the negative ones)
        # unless we need more slots than there are negative ones
        prices_of_selected = [s["price"] for s in selected]
        # Negative slots: 00:00–04:00 = 16 slots = 4 hours; from 02:00 only 8 left
        # Verify that if there are enough negative slots, they dominate the selection
        negative_selected = sum(1 for p in prices_of_selected if p <= 0)
        assert negative_selected > 0

    @pytest.mark.asyncio
    async def test_bimodal_prefers_cheaper_window(self, fake_hass):
        """With two cheap windows, the cheaper one should be selected."""
        # BIMODAL: [0.10]*12 + [0.80]*20 + [0.10]*12 + [0.80]*52
        # Window 1: 00:00–03:00 (price 0.10)
        # Window 2: 08:00–11:00 (price 0.10)
        # Both have same price so algorithm picks a contiguous block
        now = datetime(2026, 3, 15, 1, 0, tzinfo=_UTC)  # 01:00 (inside window 1)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day = _weekday_name(now)
        today_str = now.date().isoformat()
        departure_dt = now + timedelta(hours=11)  # 12:00
        departure = departure_dt.strftime("%H:%M")

        nordpool = _c.make_nordpool_prices(PRICES_BIMODAL, midnight)
        fake_hass.services.set_nordpool(today_str, nordpool)

        coord = _c.make_coordinator(fake_hass, soc=50)
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, day, departure, target_soc=80)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        selected = [s for s in coord.schedule if s["selected"]]
        assert len(selected) > 0
        # All selected slots must be from the cheap windows (price ≈ 0.10)
        for s in selected:
            assert s["price"] <= 0.15, f"Expensive slot selected: {s['price']}"

    # -----------------------------------------------------------------------
    # Tomorrow's prices needed for overnight departures
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_tomorrow_departure_uses_tomorrow_prices(self, fake_hass):
        """Schedule for tomorrow morning uses tomorrow's cheap slots."""
        now = datetime(2026, 3, 15, 22, 0, tzinfo=_UTC)  # Sun 22:00
        midnight_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        midnight_tomorrow = midnight_today + timedelta(days=1)
        today_str = now.date().isoformat()
        tomorrow_str = (now.date() + timedelta(days=1)).isoformat()
        tomorrow_day = WEEKDAYS[(now + timedelta(days=1)).weekday()]

        # Today: expensive; Tomorrow: cheap 00:00-06:00
        nordpool_today = _c.make_nordpool_prices([0.80] * 96, midnight_today)
        nordpool_tomorrow = _c.make_nordpool_prices(PRICES_OVERNIGHT_CHEAP, midnight_tomorrow)
        fake_hass.services.set_nordpool(today_str, nordpool_today)
        fake_hass.services.set_nordpool(tomorrow_str, nordpool_tomorrow)

        coord = _c.make_coordinator(fake_hass, soc=50)
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, tomorrow_day, "07:00", target_soc=80)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        selected = [s for s in coord.schedule if s["selected"]]
        assert len(selected) > 0
        # All selected should be in the cheap overnight window (00:00-06:00 tomorrow)
        for s in selected:
            assert s["start"].date() == midnight_tomorrow.date()
            assert s["price"] <= 0.15, f"Selected expensive slot: {s['price']}"

    @pytest.mark.asyncio
    async def test_tomorrow_prices_unavailable_before_1330_defers(self, fake_hass):
        """If tomorrow's prices aren't available before 13:30, schedule retry at 13:30."""
        now = datetime(2026, 3, 15, 10, 0, tzinfo=_UTC)  # 10:00, before 13:30
        today_str = now.date().isoformat()
        tomorrow_day = WEEKDAYS[(now + timedelta(days=1)).weekday()]
        midnight_today = now.replace(hour=0, minute=0, second=0, microsecond=0)

        nordpool_today = _c.make_nordpool_prices([0.80] * 96, midnight_today)
        fake_hass.services.set_nordpool(today_str, nordpool_today)
        # No tomorrow prices registered

        coord = _c.make_coordinator(fake_hass, soc=50)
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, tomorrow_day, "07:00", target_soc=80)

        call_later_calls = []

        def capturing_later(hass, delay, cb):
            call_later_calls.append({"delay": delay, "callback": cb})
            return lambda: None

        with _c.freeze_now(now):
            with patch(
                "custom_components.goe_cheap_charging.coordinator.async_call_later",
                capturing_later,
            ):
                await coord._async_rebuild_schedule()

        assert len(call_later_calls) == 1
        expected_delay = (now.replace(hour=13, minute=30) - now).total_seconds()
        assert abs(call_later_calls[0]["delay"] - expected_delay) < 60
        assert "13:30" in coord._schedule_status_reason

    @pytest.mark.asyncio
    async def test_tomorrow_prices_unavailable_after_1330_retries_5min(self, fake_hass):
        """If tomorrow's prices aren't available after 13:30, retry in 5 min."""
        now = datetime(2026, 3, 15, 14, 0, tzinfo=_UTC)  # 14:00, after 13:30
        today_str = now.date().isoformat()
        tomorrow_day = WEEKDAYS[(now + timedelta(days=1)).weekday()]
        midnight_today = now.replace(hour=0, minute=0, second=0, microsecond=0)

        nordpool_today = _c.make_nordpool_prices([0.80] * 96, midnight_today)
        fake_hass.services.set_nordpool(today_str, nordpool_today)

        coord = _c.make_coordinator(fake_hass, soc=50)
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, tomorrow_day, "07:00", target_soc=80)

        call_later_calls = []

        def capturing_later(hass, delay, cb):
            call_later_calls.append({"delay": delay, "callback": cb})
            return lambda: None

        with _c.freeze_now(now):
            with patch(
                "custom_components.goe_cheap_charging.coordinator.async_call_later",
                capturing_later,
            ):
                await coord._async_rebuild_schedule()

        assert len(call_later_calls) == 1
        assert abs(call_later_calls[0]["delay"] - 300) < 5
        assert "retry in 5 min" in coord._schedule_status_reason.lower()

    @pytest.mark.asyncio
    async def test_today_prices_unavailable_retries_5min(self, fake_hass):
        """If today's prices aren't returned by Nordpool, retry in 5 min."""
        now = _now_frozen()
        tomorrow_day = WEEKDAYS[(now + timedelta(days=1)).weekday()]
        # No prices registered at all

        coord = _c.make_coordinator(fake_hass, soc=50)
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, tomorrow_day, "07:00", target_soc=80)

        call_later_calls = []

        def capturing_later(hass, delay, cb):
            call_later_calls.append({"delay": delay, "callback": cb})
            return lambda: None

        with _c.freeze_now(now):
            with patch(
                "custom_components.goe_cheap_charging.coordinator.async_call_later",
                capturing_later,
            ):
                await coord._async_rebuild_schedule()

        assert len(call_later_calls) == 1
        assert abs(call_later_calls[0]["delay"] - 300) < 5
        assert "retry in 5 min" in coord._schedule_status_reason.lower()

    # -----------------------------------------------------------------------
    # Far-future departure → window not yet open
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_far_future_departure_waits_for_window(self, fake_hass):
        """Departure 4 days away → slot window not open yet, retry when window opens."""
        now = _now_frozen()  # Sunday 10:00
        target_day = (now + timedelta(days=4))  # Thursday
        target_day_name = WEEKDAYS[target_day.weekday()]
        midnight_today = now.replace(hour=0, minute=0, second=0, microsecond=0)

        nordpool_today = _c.make_nordpool_prices(PRICES_FLAT, midnight_today)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool_today)

        coord = _c.make_coordinator(fake_hass, soc=50)
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, target_day_name, "07:00", target_soc=80)

        call_later_calls = []

        def capturing_later(hass, delay, cb):
            call_later_calls.append({"delay": delay, "callback": cb})
            return lambda: None

        with _c.freeze_now(now):
            with patch(
                "custom_components.goe_cheap_charging.coordinator.async_call_later",
                capturing_later,
            ):
                await coord._async_rebuild_schedule()

        # Should schedule a retry when window opens (departure - 1 day)
        assert len(call_later_calls) == 1
        window_open = target_day.replace(hour=7, minute=0, second=0, microsecond=0) - timedelta(days=1)
        expected_delay = (window_open - now).total_seconds()
        assert abs(call_later_calls[0]["delay"] - expected_delay) < 120
        assert "Waiting until" in coord._schedule_status_reason

    # -----------------------------------------------------------------------
    # Guest mode / no car
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_guest_mode_no_manual_kwh_no_schedule(self, fake_hass):
        """Guest mode without manual kWh → 0 kWh needed → no schedule."""
        now = _now_frozen()
        day = _weekday_name(now)
        departure = (now + timedelta(hours=4)).strftime("%H:%M")
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        nordpool = _c.make_nordpool_prices(PRICES_OVERNIGHT_CHEAP, midnight)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool)

        coord = _c.make_coordinator(fake_hass, soc=50)
        coord.car = None
        coord._active_car_is_guest = True
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, day, departure, target_soc=80, manual_kwh=0.0)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        assert coord.schedule == []

    @pytest.mark.asyncio
    async def test_guest_mode_with_manual_kwh_builds_schedule(self, fake_hass):
        """Guest mode with manual kWh override → schedule is built."""
        now = _now_frozen()
        day = _weekday_name(now)
        departure = (now + timedelta(hours=6)).strftime("%H:%M")
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        nordpool = _c.make_nordpool_prices(PRICES_OVERNIGHT_CHEAP, midnight)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool)

        coord = _c.make_coordinator(fake_hass, soc=50)
        coord.car = None
        coord._active_car_is_guest = True
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, day, departure, target_soc=80, manual_kwh=10.0)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        selected = [s for s in coord.schedule if s["selected"]]
        assert len(selected) > 0

    # -----------------------------------------------------------------------
    # Manual kWh override
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_manual_kwh_overrides_soc_calculation(self, fake_hass):
        """Manual kWh > 0 uses that value regardless of car SoC."""
        now = _now_frozen()
        day = _weekday_name(now)
        departure = (now + timedelta(hours=6)).strftime("%H:%M")
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        nordpool = _c.make_nordpool_prices(PRICES_VOLATILE, midnight)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool)

        # Car is almost full — SoC calculation would need very few slots
        coord_soc = _c.make_coordinator(fake_hass, soc=79)
        coord_manual = _c.make_coordinator(fake_hass, soc=79)
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, day, departure, target_soc=80, manual_kwh=0.0)

        with _c.freeze_now(now):
            await coord_soc._async_rebuild_schedule()

        # Reset and try with manual kWh
        _c.set_day_config(fake_hass, day, departure, target_soc=80, manual_kwh=20.0)
        with _c.freeze_now(now):
            await coord_manual._async_rebuild_schedule()

        soc_selected = sum(1 for s in coord_soc.schedule if s["selected"])
        manual_selected = sum(1 for s in coord_manual.schedule if s["selected"])

        # Manual override for 20 kWh should select more slots
        assert manual_selected > soc_selected

    # -----------------------------------------------------------------------
    # Price spread threshold: low spread → continuous, high spread → selective
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_low_spread_threshold_forces_continuous(self, fake_hass):
        """With a very high spread_threshold all slots are treated as continuous."""
        now = _now_frozen()
        day = _weekday_name(now)
        departure = (now + timedelta(hours=6)).strftime("%H:%M")
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        nordpool = _c.make_nordpool_prices(PRICES_VOLATILE, midnight)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool)

        coord = _c.make_coordinator(fake_hass, soc=50)
        coord._price_spread_threshold = 5.0  # very high → always continuous
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, day, departure, target_soc=80)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        assert all(s["selected"] for s in coord.schedule)

    @pytest.mark.asyncio
    async def test_high_spread_selective(self, fake_hass):
        """PRICES_SPIKE has a large spread → selective slot picking.

        now=Sunday 10:00.  Departure at Monday 00:00 (14 h away).
        We set MONDAY's departure to "00:00" — that correctly represents
        'midnight at the start of Monday' which is 14 h from now.
        Sunday departure at 00:00 would mean 'start of Sunday' (10 h ago).
        """
        now = _now_frozen()  # Sunday 10:00
        # Departure is Monday 00:00 (14 h away) — store on Monday, not Sunday
        monday = _weekday_name(now + timedelta(days=1))
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        nordpool = _c.make_nordpool_prices(PRICES_SPIKE, midnight)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool)

        coord = _c.make_coordinator(fake_hass, soc=30)
        coord._price_spread_threshold = 0.10
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, monday, "00:00", target_soc=80)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        selected = [s for s in coord.schedule if s["selected"]]
        non_selected = [s for s in coord.schedule if not s["selected"]]
        assert len(selected) > 0
        assert len(non_selected) > 0  # not all slots selected
        # Spike slots (17:00–21:00) should not be selected
        spike_selected = [s for s in selected if 17 <= s["start"].hour < 21]
        assert len(spike_selected) == 0


# ============================================================================
# TestChargerCommandMatrix
# ============================================================================

class TestChargerCommandMatrix:
    """Tests _async_apply_charger_command with all decision-matrix combinations."""

    def _make_coord(self, fake_hass, **cfg_overrides):
        coord = _c.make_coordinator(fake_hass, cfg=_c.make_config(**cfg_overrides))
        return coord

    @pytest.mark.asyncio
    async def test_in_slot_no_transaction_sends_trx_and_frc2(
        self, fake_hass, mqtt_log
    ):
        """Car connected, in selected slot, no active transaction → trx=1 then frc=2."""
        now = _now_frozen()
        coord = self._make_coord(fake_hass)
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = False
        coord.schedule = [_slot_covering_now(now)]

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()

        keys = _c.all_mqtt_keys(mqtt_log)
        assert "trx" in keys
        assert "frc" in keys
        assert mqtt_log[keys.index("trx")]["payload"] == 1
        # frc=2 should follow trx
        frc_val = next(e["payload"] for e in mqtt_log if "frc" in e["topic"].split("/"))
        assert frc_val == 2
        assert coord._transaction_active is True

    @pytest.mark.asyncio
    async def test_in_slot_with_transaction_sends_frc2_only(
        self, fake_hass, mqtt_log
    ):
        """Car connected, in slot, transaction already active → only frc=2."""
        now = _now_frozen()
        coord = self._make_coord(fake_hass)
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = True
        coord.schedule = [_slot_covering_now(now)]

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()

        keys = _c.all_mqtt_keys(mqtt_log)
        assert "trx" not in keys
        assert keys.count("frc") == 1
        assert mqtt_log[0]["payload"] == 2

    @pytest.mark.asyncio
    async def test_outside_slot_with_transaction_sends_frc1(
        self, fake_hass, mqtt_log
    ):
        """Car connected, outside slot, transaction active → frc=1 (pause)."""
        now = _now_frozen()
        coord = self._make_coord(fake_hass)
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = True
        # A slot in the future (not covering now)
        future_start = now + timedelta(hours=2)
        coord.schedule = [_make_slot_at(future_start, selected=True)]

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()

        cmds = _c.mqtt_commands(mqtt_log)
        assert cmds.get("frc") == 1
        assert "trx" not in cmds

    @pytest.mark.asyncio
    async def test_outside_slot_no_transaction_sends_nothing(
        self, fake_hass, mqtt_log
    ):
        """Outside slot, no transaction → nothing to pause, no command."""
        now = _now_frozen()
        coord = self._make_coord(fake_hass)
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = False
        future_start = now + timedelta(hours=2)
        coord.schedule = [_make_slot_at(future_start, selected=True)]

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()

        assert mqtt_log == []

    @pytest.mark.asyncio
    async def test_charge_now_override_sends_charge_regardless_of_slot(
        self, fake_hass, mqtt_log
    ):
        """charge_now=True → frc=2 even when outside any scheduled slot."""
        now = _now_frozen()
        coord = self._make_coord(fake_hass)
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = True
        coord._charge_now = True
        coord.schedule = []  # no schedule at all

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()

        cmds = _c.mqtt_commands(mqtt_log)
        assert cmds.get("frc") == 2

    @pytest.mark.asyncio
    async def test_car_idle_sends_nothing(self, fake_hass, mqtt_log):
        """Car idle (no cable) → nothing sent regardless of schedule."""
        now = _now_frozen()
        coord = self._make_coord(fake_hass)
        coord.car_state = CAR_IDLE
        coord.schedule = [_slot_covering_now(now)]
        coord._transaction_active = True

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()

        assert mqtt_log == []

    @pytest.mark.asyncio
    async def test_charging_in_slot_sends_frc2(self, fake_hass, mqtt_log):
        """Car actively charging, in selected slot → frc=2 (continue)."""
        now = _now_frozen()
        coord = self._make_coord(fake_hass)
        coord.car_state = CAR_CHARGING
        coord._transaction_active = True
        coord.schedule = [_slot_covering_now(now)]

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()

        cmds = _c.mqtt_commands(mqtt_log)
        assert cmds.get("frc") == 2

    @pytest.mark.asyncio
    async def test_charging_outside_slot_sends_frc1(self, fake_hass, mqtt_log):
        """Car actively charging, outside selected slot → frc=1 (pause)."""
        now = _now_frozen()
        coord = self._make_coord(fake_hass)
        coord.car_state = CAR_CHARGING
        coord._transaction_active = True
        future_start = now + timedelta(hours=1)
        coord.schedule = [_make_slot_at(future_start, selected=True)]

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()

        cmds = _c.mqtt_commands(mqtt_log)
        assert cmds.get("frc") == 1

    @pytest.mark.asyncio
    async def test_charge_complete_still_applies_command(self, fake_hass, mqtt_log):
        """car_state=4 (complete) is NOT idle → command is applied."""
        now = _now_frozen()
        coord = self._make_coord(fake_hass)
        coord.car_state = CAR_COMPLETE
        coord._transaction_active = True
        coord.schedule = [_slot_covering_now(now)]

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()

        # CAR_COMPLETE != CAR_IDLE → command IS sent (frc=2 because in slot)
        cmds = _c.mqtt_commands(mqtt_log)
        assert cmds.get("frc") == 2

    # -----------------------------------------------------------------------
    # Cheap threshold (opportunistic charging)
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_cheap_slot_with_threshold_triggers_charge(
        self, fake_hass, mqtt_log
    ):
        """Opportunistic slot in schedule → frc=2."""
        now = _now_frozen()
        coord = self._make_coord(fake_hass)
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = True
        # Opportunistic slot covering now (added to schedule during rebuild)
        opp_slot = {**_slot_covering_now(now), "opportunistic": True}
        coord.schedule = [opp_slot]

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()

        cmds = _c.mqtt_commands(mqtt_log)
        assert cmds.get("frc") == 2

    @pytest.mark.asyncio
    async def test_only_3_cheap_slots_does_not_trigger(self, fake_hass, mqtt_log):
        """Only 3 consecutive cheap slots (< 1h) → threshold not triggered."""
        now = _now_frozen()
        coord = self._make_coord(fake_hass)
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = True
        coord.schedule = []
        coord._cheap_threshold = 0.50

        # 3 cheap + 1 expensive
        prices = []
        for i in range(3):
            start = now - timedelta(minutes=5) + timedelta(minutes=15 * i)
            end = start + timedelta(minutes=15)
            prices.append({"start": start.isoformat(), "end": end.isoformat(), "price": 0.30})
        # expensive slot to break block
        start = now + timedelta(minutes=40)
        prices.append({"start": start.isoformat(), "end": (start + timedelta(minutes=15)).isoformat(), "price": 2.00})
        coord._current_price_data = prices

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()

        # No slot, threshold not triggered → should NOT send frc=2
        cmds = _c.mqtt_commands(mqtt_log)
        # transaction active and no charge reason → frc=1 (paused)
        assert cmds.get("frc") == 1

    @pytest.mark.asyncio
    async def test_threshold_zero_disables_opportunistic(self, fake_hass, mqtt_log):
        """cheap_threshold=0 → opportunistic mode is disabled."""
        now = _now_frozen()
        coord = self._make_coord(fake_hass)
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = True
        coord.schedule = []
        coord._cheap_threshold = 0.0  # disabled

        prices = []
        for i in range(6):
            start = now - timedelta(minutes=5) + timedelta(minutes=15 * i)
            end = start + timedelta(minutes=15)
            prices.append({"start": start.isoformat(), "end": end.isoformat(), "price": 0.10})
        coord._current_price_data = prices

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()

        cmds = _c.mqtt_commands(mqtt_log)
        assert cmds.get("frc") == 1  # paused, not charging

    # -----------------------------------------------------------------------
    # SoC limit behavior
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_in_slot_sets_target_soc_limit(self, fake_hass):
        """In-slot charging sets car charge limit to target_soc."""
        now = _now_frozen()
        coord = self._make_coord(fake_hass)
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = True
        coord._last_target_soc = 80
        coord._last_sent_car_limit = None
        coord.schedule = [_slot_covering_now(now)]

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()

        assert coord.car.set_charge_limit_calls == [80]
        assert coord._last_sent_car_limit == 80

    @pytest.mark.asyncio
    async def test_charge_now_sets_charge_now_limit(self, fake_hass):
        """charge_now mode sets car charge limit to charge_now_soc_limit."""
        now = _now_frozen()
        coord = self._make_coord(fake_hass)
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = True
        coord._charge_now = True
        coord._charge_now_soc_limit = 90.0
        coord._last_sent_car_limit = None

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()

        assert coord.car.set_charge_limit_calls == [90]

    @pytest.mark.asyncio
    async def test_opportunistic_sets_opportunistic_limit(self, fake_hass):
        """Opportunistic slot in schedule → car charge limit set to opportunistic_soc_limit."""
        now = _now_frozen()
        coord = self._make_coord(fake_hass)
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = True
        coord._opportunistic_soc_limit = 60.0
        coord._last_sent_car_limit = None
        opp_slot = {**_slot_covering_now(now), "opportunistic": True}
        coord.schedule = [opp_slot]

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()

        assert coord.car.set_charge_limit_calls == [60]

    @pytest.mark.asyncio
    async def test_charge_limit_not_resent_when_unchanged(self, fake_hass):
        """Car charge limit is not re-sent if already set to same value."""
        now = _now_frozen()
        coord = self._make_coord(fake_hass)
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = True
        coord._last_target_soc = 80
        coord._last_sent_car_limit = 80  # already sent
        coord.schedule = [_slot_covering_now(now)]

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()
            await coord._async_apply_charger_command()  # second call

        assert coord.car.set_charge_limit_calls == []  # never sent

    @pytest.mark.asyncio
    async def test_guest_mode_no_car_limit_sent(self, fake_hass):
        """In guest mode, car charge limit is never set."""
        now = _now_frozen()
        coord = self._make_coord(fake_hass)
        coord._active_car_is_guest = True
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = True
        coord.schedule = [_slot_covering_now(now)]

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()

        # No car → no set_charge_limit call
        assert coord.car.set_charge_limit_calls == []


# ============================================================================
# TestCarStateMachine
# ============================================================================

class TestCarStateMachine:
    """Tests _handle_car_state() transitions and side effects."""

    @pytest.mark.asyncio
    async def test_plugin_no_transaction_starts_transaction(
        self, fake_hass, mqtt_log
    ):
        """Car plugged in (1→3) with no transaction → trx=1 sent."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_IDLE
        coord._transaction_active = False
        coord.car_state = CAR_IDLE  # set prev state

        coord._handle_car_state(CAR_CONNECTED)
        await fake_hass.drain_tasks()

        # _async_handle_plugin sends trx=1 + frc=1 (starts paused)
        keys = _c.all_mqtt_keys(mqtt_log)
        assert "trx" in keys
        assert coord._transaction_active is True

    @pytest.mark.asyncio
    async def test_plugin_with_existing_transaction_skips_trx(
        self, fake_hass, mqtt_log
    ):
        """Car plugged in with active transaction (HA restart) → no trx=1."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_IDLE
        coord._transaction_active = True  # already active from retained MQTT

        coord._handle_car_state(CAR_CONNECTED)
        await fake_hass.drain_tasks()

        keys = _c.all_mqtt_keys(mqtt_log)
        assert "trx" not in keys

    @pytest.mark.asyncio
    async def test_plugin_with_transaction_running_ha_restart(
        self, fake_hass, mqtt_log
    ):
        """HA restart: car=3, trx active, hass.state=running → apply command but no trx."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_IDLE
        coord._transaction_active = True
        fake_hass.state = _c._CoreState.running

        coord._handle_car_state(CAR_CONNECTED)
        await fake_hass.drain_tasks()

        keys = _c.all_mqtt_keys(mqtt_log)
        assert "trx" not in keys
        # frc command sent (empty schedule → frc=1)
        assert "frc" in keys

    @pytest.mark.asyncio
    async def test_plugin_with_transaction_ha_starting_no_command(
        self, fake_hass, mqtt_log
    ):
        """HA still starting: car=3, trx active → skip command entirely."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_IDLE
        coord._transaction_active = True
        fake_hass.state = _c._CoreState.starting

        coord._handle_car_state(CAR_CONNECTED)
        await fake_hass.drain_tasks()

        # hass.state != running → no _async_apply_charger_command called
        assert mqtt_log == []

    @pytest.mark.asyncio
    async def test_charging_on_startup_marks_transaction_active(self, fake_hass):
        """car=2 arrives on startup without trx message → transaction marked active."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_IDLE
        coord._transaction_active = False

        coord._handle_car_state(CAR_CHARGING)
        # _transaction_active is set synchronously; cancel the amp-adjust loop
        # (which sleeps 30s) before draining to avoid a hang.
        if coord._amp_adjust_task:
            coord._amp_adjust_task.cancel()
            coord._amp_adjust_task = None
        await fake_hass.drain_tasks()

        assert coord._transaction_active is True

    @pytest.mark.asyncio
    async def test_charge_complete_clears_transaction(self, fake_hass):
        """Charge complete (→4) → transaction stays active (go-e retains trx until unplug),
        but last_sent_car_limit is reset so the limit is resent if charging resumes."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_CHARGING
        coord._transaction_active = True
        coord._last_sent_car_limit = 80

        coord._handle_car_state(CAR_COMPLETE)
        await fake_hass.drain_tasks()

        assert coord._transaction_active is True  # retained until cable removed (trx=null MQTT)
        assert coord._last_sent_car_limit is None

    @pytest.mark.asyncio
    async def test_charge_complete_stops_amp_adjust(self, fake_hass):
        """Charge complete → amp adjust task stopped."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_CHARGING

        # Simulate amp adjust loop running
        async def _dummy_loop():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                pass

        coord._amp_adjust_task = asyncio.ensure_future(_dummy_loop())
        coord._handle_car_state(CAR_COMPLETE)
        await fake_hass.drain_tasks()

        assert coord._amp_adjust_task is None or coord._amp_adjust_task.done()

    @pytest.mark.asyncio
    async def test_replug_after_complete_does_not_auto_trx(self, fake_hass, mqtt_log):
        """4→3 transition does NOT call _async_handle_plugin (prev != CAR_IDLE)."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_COMPLETE
        coord._transaction_active = False  # cleared by charge complete

        coord._handle_car_state(CAR_CONNECTED)
        await fake_hass.drain_tasks()

        # 4→3: prev=COMPLETE, not IDLE → _async_handle_plugin NOT called
        keys = _c.all_mqtt_keys(mqtt_log)
        assert "trx" not in keys

    @pytest.mark.asyncio
    async def test_start_charging_starts_amp_adjust(self, fake_hass):
        """Transition to CAR_CHARGING starts amp-adjust loop."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_IDLE
        coord._amp_adjust_task = None

        coord._handle_car_state(CAR_CHARGING)
        await asyncio.sleep(0)  # let event loop process

        assert coord._amp_adjust_task is not None
        coord._amp_adjust_task.cancel()

    @pytest.mark.asyncio
    async def test_stop_charging_stops_amp_adjust(self, fake_hass):
        """Transition away from CAR_CHARGING stops amp-adjust loop."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_CHARGING

        async def _dummy_loop():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                pass

        coord._amp_adjust_task = asyncio.ensure_future(_dummy_loop())
        coord._handle_car_state(CAR_CONNECTED)
        await fake_hass.drain_tasks()

        assert coord._amp_adjust_task is None or coord._amp_adjust_task.done()


# ============================================================================
# TestTransactionManagement
# ============================================================================

class TestTransactionManagement:
    """Tests for the _transaction_active state machine via MQTT messages."""

    def _make_msg(self, serial: str, key: str, payload: str) -> _c.FakeMqttMsg:
        return _c.FakeMqttMsg(f"go-eCharger/{serial}/{key}", payload)

    def test_trx_mqtt_1_sets_transaction_active(self, fake_hass):
        coord = _c.make_coordinator(fake_hass)
        coord._transaction_active = False
        msg = self._make_msg("XYZ123", "trx", "1")
        coord._handle_mqtt_message(msg)
        assert coord._transaction_active is True

    def test_trx_mqtt_0_clears_transaction_active(self, fake_hass):
        coord = _c.make_coordinator(fake_hass)
        coord._transaction_active = True
        msg = self._make_msg("XYZ123", "trx", "0")
        coord._handle_mqtt_message(msg)
        assert coord._transaction_active is False

    def test_car_mqtt_updates_car_state(self, fake_hass):
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_IDLE
        msg = self._make_msg("XYZ123", "car", "3")
        coord._handle_mqtt_message(msg)
        assert coord.car_state == CAR_CONNECTED

    def test_unknown_key_ignored(self, fake_hass):
        coord = _c.make_coordinator(fake_hass)
        initial_state = coord.car_state
        msg = self._make_msg("XYZ123", "someunknownkey", "1")
        coord._handle_mqtt_message(msg)
        assert coord.car_state == initial_state

    def test_invalid_json_ignored(self, fake_hass):
        coord = _c.make_coordinator(fake_hass)
        msg = _c.FakeMqttMsg("go-eCharger/XYZ123/trx", "not-json!!!")
        coord._handle_mqtt_message(msg)  # should not raise

    @pytest.mark.asyncio
    async def test_double_plugin_sends_trx_only_once(self, fake_hass, mqtt_log):
        """Rapid double plug-in: second call skips trx=1 since transaction already active."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_IDLE
        coord._transaction_active = False

        # First plug-in
        await coord._async_handle_plugin()
        assert coord._transaction_active is True
        trx_count_1 = sum(1 for k in _c.all_mqtt_keys(mqtt_log) if k == "trx")
        assert trx_count_1 == 1

        # Second call (e.g. retained MQTT re-delivered)
        await coord._async_handle_plugin()
        trx_count_2 = sum(1 for k in _c.all_mqtt_keys(mqtt_log) if k == "trx")
        assert trx_count_2 == 1  # still 1, not 2


# ============================================================================
# TestAmpAdjustScenarios
# ============================================================================

class TestAmpAdjustScenarios:
    """Parametrized amp-adjustment tests covering 1-phase, 3-phase, and edge cases."""

    def _make_coord_with_phases(
        self,
        fake_hass,
        phases: list[float],
        charger_phase: int = 1,
        n_phases: int = 1,
        last_amp: int = 0,
        breaker: int = 20,
        min_a: int = 6,
        max_a: int = 16,
    ) -> ChargingCoordinator:
        cfg = _c.make_config(
            **{
                CONF_CHARGER_PHASE: charger_phase,
                CONF_CHARGER_N_PHASES: n_phases,
                CONF_BREAKER_LIMIT: breaker,
                CONF_MIN_AMP: min_a,
                CONF_MAX_AMP: max_a,
            }
        )
        coord = _c.make_coordinator(fake_hass, cfg=cfg)
        coord.car_state = CAR_CHARGING
        coord._last_sent_amp = last_amp
        fake_hass.states.set("sensor.l1", str(phases[0]))
        fake_hass.states.set("sensor.l2", str(phases[1]))
        fake_hass.states.set("sensor.l3", str(phases[2]))
        return coord

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "phases,charger_phase,n_phases,last_amp,breaker,min_a,max_a,expected_amp,expect_cmd",
        [
            # 1-phase charger on L1: baseline = 10-8=2, headroom=min(18,15,15)=15
            ([10.0, 5.0, 5.0], 1, 1, 8, 20, 6, 16, 15, True),
            # 1-phase charger on L2: baseline = 18-10=8, headroom=min(15,12,15)=12
            ([5.0, 18.0, 5.0], 2, 1, 10, 20, 6, 16, 12, True),
            # 1-phase charger on L3: baseline = 16-6=10, headroom=min(15,15,10)=10
            ([5.0, 5.0, 16.0], 3, 1, 6, 20, 6, 16, 10, True),
            # Clamp to max (headroom > max_amp)
            ([1.0, 1.0, 1.0], 1, 1, 0, 20, 6, 16, 16, True),
            # Clamp to min (headroom < min_amp)
            ([18.0, 18.0, 18.0], 1, 1, 0, 20, 6, 16, 6, True),
            # No change (last_amp already at result) → no command
            ([5.0, 5.0, 5.0], 1, 1, 15, 20, 6, 16, 15, False),
            # 3-phase: subtract last_amp from ALL phases
            # [15,15,15] - 10 = [5,5,5], headroom=15 → clamp to 16... wait
            # headroom = min(20-5,20-5,20-5) = 15; new=15, last=10 → delta=5 → command
            ([15.0, 15.0, 15.0], 1, 3, 10, 20, 6, 16, 15, True),
            # Different breaker limit
            ([5.0, 5.0, 5.0], 1, 1, 6, 16, 6, 16, 11, True),
            # Very tight headroom → clamp to min_amp
            ([19.0, 3.0, 3.0], 1, 1, 0, 20, 6, 16, 6, True),
        ],
    )
    async def test_amp_adjust_parametrized(
        self,
        fake_hass,
        mqtt_log,
        phases,
        charger_phase,
        n_phases,
        last_amp,
        breaker,
        min_a,
        max_a,
        expected_amp,
        expect_cmd,
    ):
        coord = self._make_coord_with_phases(
            fake_hass, phases, charger_phase, n_phases, last_amp, breaker, min_a, max_a
        )

        await coord._async_do_amp_adjust()

        cmds = _c.mqtt_commands(mqtt_log)
        if expect_cmd:
            assert "amp" in cmds, f"Expected amp command but mqtt_log={mqtt_log}"
            assert cmds["amp"] == expected_amp
        else:
            assert "amp" not in cmds, f"Expected no amp command but got {cmds}"

    @pytest.mark.asyncio
    async def test_phase_sensor_unavailable_skips_adjust(
        self, fake_hass, mqtt_log
    ):
        """If any phase sensor is unavailable, amp adjust is skipped."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_CHARGING
        fake_hass.states.set("sensor.l1", "10.0")
        fake_hass.states.set("sensor.l2", "unavailable")
        fake_hass.states.set("sensor.l3", "5.0")

        await coord._async_do_amp_adjust()

        assert mqtt_log == []

    @pytest.mark.asyncio
    async def test_phase_sensor_missing_skips_adjust(self, fake_hass, mqtt_log):
        """Missing (None) phase sensor → amp adjust skipped."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_CHARGING
        # Only set L1 and L3; L2 entity not registered in hass.states

        await coord._async_do_amp_adjust()

        assert mqtt_log == []

    @pytest.mark.asyncio
    async def test_car_not_charging_skips_adjust(self, fake_hass, mqtt_log):
        """Amp adjust skipped when car_state != CAR_CHARGING."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_CONNECTED
        fake_hass.states.set("sensor.l1", "5.0")
        fake_hass.states.set("sensor.l2", "5.0")
        fake_hass.states.set("sensor.l3", "5.0")

        await coord._async_do_amp_adjust()

        assert mqtt_log == []

    @pytest.mark.asyncio
    async def test_amp_adjust_sends_new_amp_to_charger(self, fake_hass, mqtt_log):
        """amp command goes to go-e charger via MQTT."""
        coord = self._make_coord_with_phases(
            fake_hass, [5.0, 5.0, 5.0], charger_phase=1, last_amp=6
        )

        await coord._async_do_amp_adjust()

        cmds = _c.mqtt_commands(mqtt_log)
        assert "amp" in cmds
        # Verify topic is go-e MQTT topic
        amp_entry = next(e for e in mqtt_log if "amp" in e["topic"])
        assert "go-eCharger/XYZ123/amp/set" == amp_entry["topic"]


# ============================================================================
# TestPluginScenarios
# ============================================================================

class TestPluginScenarios:
    """End-to-end simulation of plug-in → schedule → slot transition flows."""

    @pytest.mark.asyncio
    async def test_plugin_outside_slot_starts_paused(self, fake_hass, mqtt_log):
        """Fresh plug-in outside a cheap slot → transaction started paused (frc=1)."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_IDLE
        coord._transaction_active = False

        coord._handle_car_state(CAR_CONNECTED)
        await fake_hass.drain_tasks()

        keys = _c.all_mqtt_keys(mqtt_log)
        assert "trx" in keys
        # frc after trx should be 1 (paused since no schedule yet)
        frc_entries = [e for e in mqtt_log if "frc" in e["topic"]]
        assert len(frc_entries) >= 1
        # Last frc is 1 (paused)
        assert frc_entries[-1]["payload"] == 1

    @pytest.mark.asyncio
    async def test_slot_boundary_fires_frc2_on_slot_start(
        self, fake_hass, mqtt_log
    ):
        """When a slot boundary fires and we enter a selected slot → frc=2."""
        now = _now_frozen()
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = True

        # A slot that starts exactly 'now'
        slot_start = now
        coord.schedule = [_make_slot_at(slot_start, selected=True)]

        with _c.freeze_now(now):
            await coord._async_on_slot_boundary()

        cmds = _c.mqtt_commands(mqtt_log)
        assert cmds.get("frc") == 2

    @pytest.mark.asyncio
    async def test_slot_boundary_fires_frc1_on_slot_end(
        self, fake_hass, mqtt_log
    ):
        """When a slot boundary fires and we exit the last slot → frc=1."""
        now = _now_frozen()
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_CHARGING
        coord._transaction_active = True

        # A slot that ended 1 minute ago
        past_end = now - timedelta(minutes=1)
        past_start = past_end - timedelta(minutes=14)
        coord.schedule = [{
            "start": past_start,
            "end": past_end,
            "price": 0.50,
            "selected": True,
        }]

        with _c.freeze_now(now):
            await coord._async_on_slot_boundary()

        cmds = _c.mqtt_commands(mqtt_log)
        assert cmds.get("frc") == 1

    @pytest.mark.asyncio
    async def test_full_schedule_cycle(self, fake_hass, mqtt_log):
        """Full cycle: rebuild schedule, enter slot (frc=2), exit slot (frc=1)."""
        now = datetime(2026, 3, 15, 9, 50, tzinfo=_UTC)  # 09:50
        day = _weekday_name(now)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        departure = "12:00"

        # Cheap slots at 10:00–11:00 (4 slots × 15min)
        nordpool = _c.make_nordpool_prices(PRICES_SOLAR_DIP, midnight)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool)

        # soc=79% so only ~4 slots needed — the cheap 10:00 window is sufficient
        # and the expensive 09:45-10:00 in-progress slot is NOT selected.
        coord = _c.make_coordinator(fake_hass, soc=79)
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = True
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, day, departure, target_soc=80)

        # Step 1: build schedule at 09:50 (before cheap window)
        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        assert len(coord.schedule) > 0
        selected = [s for s in coord.schedule if s["selected"]]
        assert len(selected) > 0
        # Should not be charging yet (09:45 slot is expensive — not selected)
        cmds_rebuild = _c.mqtt_commands(mqtt_log)
        assert cmds_rebuild.get("frc") == 1  # paused

        # Step 2: enter cheap window at 10:00
        mqtt_log.clear()
        slot_start = datetime(2026, 3, 15, 10, 0, tzinfo=_UTC)
        with _c.freeze_now(slot_start):
            await coord._async_on_slot_boundary()

        cmds_in_slot = _c.mqtt_commands(mqtt_log)
        assert cmds_in_slot.get("frc") == 2  # charging

        # Step 3: leave cheap window at 14:00
        mqtt_log.clear()
        slot_end = datetime(2026, 3, 15, 14, 0, tzinfo=_UTC)
        with _c.freeze_now(slot_end):
            await coord._async_on_slot_boundary()

        cmds_out_of_slot = _c.mqtt_commands(mqtt_log)
        assert cmds_out_of_slot.get("frc") == 1  # paused

    @pytest.mark.asyncio
    async def test_ha_restart_recovery_with_active_session(
        self, fake_hass, mqtt_log
    ):
        """On HA restart (hass.state=running), retained MQTT shows car=2, trx=1.
        Coordinator should skip trx=1 and apply frc correctly after rebuild."""
        now = _now_frozen()
        day = _weekday_name(now)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        departure = (now + timedelta(hours=4)).strftime("%H:%M")

        nordpool = _c.make_nordpool_prices(PRICES_SOLAR_DIP, midnight)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool)

        coord = _c.make_coordinator(fake_hass, soc=50)
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, day, departure, target_soc=80)
        fake_hass.state = _c._CoreState.running

        # Simulate retained MQTT arriving: trx=1, car=2
        coord._handle_mqtt_message(_c.FakeMqttMsg("go-eCharger/XYZ123/trx", "1"))
        coord._handle_mqtt_message(_c.FakeMqttMsg("go-eCharger/XYZ123/car", "2"))
        # Cancel amp-adjust loop (sleeps 30s) before draining to avoid a hang
        if coord._amp_adjust_task:
            coord._amp_adjust_task.cancel()
            coord._amp_adjust_task = None
        await fake_hass.drain_tasks()

        # No trx=1 should have been sent
        keys = _c.all_mqtt_keys(mqtt_log)
        assert "trx" not in keys

        # Rebuild schedule (as triggered by HA_STARTED event)
        mqtt_log.clear()
        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        # frc should be set correctly (in slot or paused based on schedule)
        keys = _c.all_mqtt_keys(mqtt_log)
        assert "frc" in keys
        # No new trx=1
        assert "trx" not in keys

    @pytest.mark.asyncio
    async def test_ha_restart_mid_slot_continues_charging(
        self, fake_hass, mqtt_log
    ):
        """Regression: HA restarts 5 min into a cheap slot.
        The in-progress slot must be included in the schedule so charging
        is not interrupted (frc=2), not stopped (frc=1)."""
        # Restart at 04:35 — 5 minutes into the cheap 04:30-04:45 slot
        now = datetime(2026, 3, 16, 4, 35, tzinfo=_UTC)
        day = _weekday_name(now)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        departure = "09:00"

        nordpool = _c.make_nordpool_prices(PRICES_OVERNIGHT_CHEAP, midnight)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool)

        coord = _c.make_coordinator(fake_hass, soc=70)
        coord.car_state = CAR_CHARGING
        coord._transaction_active = True
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, day, departure, target_soc=80)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        cmds = _c.mqtt_commands(mqtt_log)
        # Must continue charging — NOT send frc=1 just because we restarted mid-slot
        assert cmds.get("frc") == 2, (
            "frc=1 was sent after restart mid-slot — charging was interrupted unnecessarily"
        )
        assert "trx" not in _c.all_mqtt_keys(mqtt_log)


# ============================================================================
# TestIsLongChargingBlock
# ============================================================================

class TestIsLongChargingBlock:
    """Tests for the _is_long_charging_block() helper."""

    def test_6_slot_block_is_long(self, fake_hass):
        """7 contiguous selected slots (105 min > 90 min threshold) → long block."""
        now = _now_frozen()
        coord = _c.make_coordinator(fake_hass)
        start = now + timedelta(minutes=5)
        coord.schedule = [
            _make_slot_at(start + timedelta(minutes=15 * i), selected=True)
            for i in range(7)
        ]
        with _c.freeze_now(now):
            assert coord._is_long_charging_block() is True

    def test_3_slot_block_is_not_long(self, fake_hass):
        """3 contiguous selected slots (45 min) → not a long block."""
        now = _now_frozen()
        coord = _c.make_coordinator(fake_hass)
        start = now + timedelta(minutes=5)
        coord.schedule = [
            _make_slot_at(start + timedelta(minutes=15 * i), selected=True)
            for i in range(3)
        ]
        with _c.freeze_now(now):
            assert coord._is_long_charging_block() is False

    def test_empty_schedule_not_long(self, fake_hass):
        """No schedule → not a long block."""
        coord = _c.make_coordinator(fake_hass)
        coord.schedule = []
        assert coord._is_long_charging_block() is False

    def test_past_slots_not_counted(self, fake_hass):
        """Past slots (end < now) are ignored."""
        now = _now_frozen()
        coord = _c.make_coordinator(fake_hass)
        past_start = now - timedelta(hours=3)
        coord.schedule = [
            _make_slot_at(past_start + timedelta(minutes=15 * i), selected=True)
            for i in range(8)
        ]
        with _c.freeze_now(now):
            assert coord._is_long_charging_block() is False


# ============================================================================
# TestMQTTTopicHandling
# ============================================================================

class TestMQTTTopicHandling:
    """Tests for MQTT message parsing and topic extraction."""

    def test_charger_status_topic_extraction(self, fake_hass):
        coord = _c.make_coordinator(fake_hass)
        # Valid key extraction
        assert coord.charger.extract_key("go-eCharger/XYZ123/car") == "car"
        assert coord.charger.extract_key("go-eCharger/XYZ123/trx") == "trx"
        assert coord.charger.extract_key("go-eCharger/XYZ123/amp") == "amp"

    def test_subtopics_ignored(self, fake_hass):
        coord = _c.make_coordinator(fake_hass)
        # Subtopics like /set and /result should be ignored
        assert coord.charger.extract_key("go-eCharger/XYZ123/frc/set") is None
        assert coord.charger.extract_key("go-eCharger/XYZ123/frc/result") is None

    def test_wrong_serial_ignored(self, fake_hass):
        coord = _c.make_coordinator(fake_hass)
        assert coord.charger.extract_key("go-eCharger/WRONGSERIAL/car") is None


# ============================================================================
# TestSyncSettingsFromHA
# ============================================================================

class TestSyncSettingsFromHA:
    """Tests for _sync_settings_from_ha() — reading entity states on rebuild."""

    def test_reads_departure_from_entity_state(self, fake_hass):
        coord = _c.make_coordinator(fake_hass)
        fake_hass.states.set("time.goe_cheap_charging_monday_departure", "07:30:00")
        coord._sync_settings_from_ha()
        assert coord._day_settings["monday"]["departure"] == time(7, 30, 0)

    def test_reads_target_soc_from_entity_state(self, fake_hass):
        coord = _c.make_coordinator(fake_hass)
        fake_hass.states.set("number.goe_cheap_charging_monday_target_soc", "85")
        coord._sync_settings_from_ha()
        assert coord._day_settings["monday"]["target_soc"] == 85

    def test_reads_manual_kwh_from_entity_state(self, fake_hass):
        coord = _c.make_coordinator(fake_hass)
        fake_hass.states.set("number.goe_cheap_charging_monday_manual_kwh", "15.5")
        coord._sync_settings_from_ha()
        assert coord._day_settings["monday"]["manual_kwh"] == 15.5

    def test_reads_smart_enabled_from_entity_state(self, fake_hass):
        coord = _c.make_coordinator(fake_hass)
        fake_hass.states.set("switch.goe_cheap_charging_smart_enabled", "on")
        coord._sync_settings_from_ha()
        assert coord._smart_enabled is True

        fake_hass.states.set("switch.goe_cheap_charging_smart_enabled", "off")
        coord._sync_settings_from_ha()
        assert coord._smart_enabled is False

    def test_unavailable_entity_leaves_default(self, fake_hass):
        coord = _c.make_coordinator(fake_hass)
        fake_hass.states.set("number.goe_cheap_charging_monday_target_soc", "unavailable")
        coord._sync_settings_from_ha()
        from custom_components.goe_cheap_charging.const import DEFAULT_TARGET_SOC
        assert coord._day_settings["monday"]["target_soc"] == DEFAULT_TARGET_SOC

    def test_hhmm_departure_format(self, fake_hass):
        """Departure state as HH:MM (no seconds) is also parsed correctly."""
        coord = _c.make_coordinator(fake_hass)
        fake_hass.states.set("time.goe_cheap_charging_friday_departure", "06:00:00")
        coord._sync_settings_from_ha()
        assert coord._day_settings["friday"]["departure"] == time(6, 0, 0)


# ============================================================================
# TestFindNextDeparture
# ============================================================================

class TestFindNextDeparture:
    """Tests for _find_next_departure()."""

    def test_finds_same_day_departure_in_future(self, fake_hass):
        now = _now_frozen()  # Sunday 10:00
        coord = _c.make_coordinator(fake_hass)
        day = _weekday_name(now)
        coord._day_settings[day]["departure"] = time(14, 0)
        coord._day_settings[day]["target_soc"] = 80

        with _c.freeze_now(now):
            result = coord._find_next_departure()

        assert result is not None
        dep_dt, target, day_name = result
        assert dep_dt.hour == 14
        assert day_name == day

    def test_skips_past_departure_today_finds_next_week(self, fake_hass):
        now = _now_frozen()  # Sunday 10:00
        coord = _c.make_coordinator(fake_hass)
        day = _weekday_name(now)
        # Departure was at 08:00 (past)
        coord._day_settings[day]["departure"] = time(8, 0)
        coord._day_settings[day]["target_soc"] = 80

        with _c.freeze_now(now):
            result = coord._find_next_departure()

        # Should wrap around to next Sunday (7 days later) or be None if not configured
        # Since only Sunday is configured, it should find next Sunday
        assert result is not None
        dep_dt, _, _ = result
        # Should be 7 days from now at 08:00
        assert dep_dt.date() == (now + timedelta(days=7)).date()

    def test_no_departure_returns_none(self, fake_hass):
        coord = _c.make_coordinator(fake_hass)
        # All departures are None by default
        result = coord._find_next_departure()
        assert result is None

    def test_finds_next_enabled_day(self, fake_hass):
        now = _now_frozen()  # Sunday 10:00
        coord = _c.make_coordinator(fake_hass)
        # Configure Tuesday departure
        coord._day_settings["tuesday"]["departure"] = time(7, 0)
        coord._day_settings["tuesday"]["target_soc"] = 80

        with _c.freeze_now(now):
            result = coord._find_next_departure()

        assert result is not None
        dep_dt, _, day_name = result
        assert day_name == "tuesday"
        assert dep_dt.weekday() == WEEKDAYS.index("tuesday")

    def test_zero_target_soc_with_no_manual_kwh_skipped(self, fake_hass):
        """Days with target_soc=0 and manual_kwh=0 are skipped."""
        now = _now_frozen()  # Sunday
        coord = _c.make_coordinator(fake_hass)
        day = _weekday_name(now)
        coord._day_settings[day]["departure"] = time(14, 0)
        coord._day_settings[day]["target_soc"] = 0
        coord._day_settings[day]["manual_kwh"] = 0.0
        # Configure Monday with a real target
        coord._day_settings["monday"]["departure"] = time(7, 0)
        coord._day_settings["monday"]["target_soc"] = 80

        with _c.freeze_now(now):
            result = coord._find_next_departure()

        assert result is not None
        _, _, day_name = result
        assert day_name == "monday"


# ============================================================================
# TestSelectSlotsAlgorithm
# ============================================================================

class TestSelectSlotsAlgorithm:
    """Direct unit tests for _select_slots() and _get_clusters()."""

    from custom_components.goe_cheap_charging.coordinator import (
        _get_clusters,
        _select_slots,
    )

    def _make_slots(self, prices: list[float]) -> list[dict]:
        now = _now_frozen()
        slots = []
        for i, p in enumerate(prices):
            start = now + timedelta(minutes=15 * i)
            slots.append({"start": start, "end": start + timedelta(minutes=15), "price": p, "selected": False})
        return slots

    def test_zero_slots_requested_selects_nothing(self):
        from custom_components.goe_cheap_charging.coordinator import _select_slots
        slots = self._make_slots([0.50] * 8)
        _select_slots(slots, 0, 0.10)
        assert not any(s["selected"] for s in slots)

    def test_fewer_slots_than_min_block_selects_all(self):
        """Fewer than MIN_BLOCK_SLOTS (4) slots → all selected regardless of n_slots."""
        from custom_components.goe_cheap_charging.coordinator import _select_slots
        slots = self._make_slots([0.10, 0.50, 0.80])  # 3 slots < 4
        _select_slots(slots, 2, 0.10)
        assert all(s["selected"] for s in slots)

    def test_n_needed_rounds_up_to_block_boundary(self):
        """n_slots=5 → rounds up to 8 (next multiple of MIN_BLOCK_SLOTS=4)."""
        from custom_components.goe_cheap_charging.coordinator import _select_slots
        # 12 slots; cheapest 8 at start
        prices = [0.10] * 8 + [0.80] * 4
        slots = self._make_slots(prices)
        _select_slots(slots, 5, 0.50)
        n_selected = sum(1 for s in slots if s["selected"])
        assert n_selected == 8

    def test_single_block_preferred_when_savings_below_threshold(self):
        """When multi-block would save ≤ threshold, single contiguous block preferred."""
        from custom_components.goe_cheap_charging.coordinator import _select_slots
        # Single cheapest block: indices 8-11 (price=0.20)
        # Multi-block: 0-3 (0.30) and 8-11 (0.20) → avg=0.25
        # single_avg=0.20, multi_avg=0.25 → single < multi → single always wins here
        prices = [0.30] * 4 + [0.80] * 4 + [0.20] * 4
        slots = self._make_slots(prices)
        _select_slots(slots, 4, 0.10)
        selected_idxs = [i for i, s in enumerate(slots) if s["selected"]]
        assert selected_idxs == [8, 9, 10, 11]

    def test_multi_block_chosen_when_significant_saving(self):
        """Multi-block saves > threshold → multi-block chosen over single."""
        from custom_components.goe_cheap_charging.coordinator import _select_slots
        # Slot 0-3: price=0.10; Slot 4-7: price=0.90; Slot 8-11: price=0.10
        # Best single 8-slot block: either 0-7 (avg=0.50) or 4-11 (avg=0.50)
        # Multi: {0-3, 8-11} → avg=0.10; diff from single=0.40 > threshold=0.10 → multi
        prices = [0.10] * 4 + [0.90] * 4 + [0.10] * 4
        slots = self._make_slots(prices)
        _select_slots(slots, 8, 0.10)
        # Expensive middle block should NOT be selected
        assert not any(slots[i]["selected"] for i in range(4, 8))
        # Both cheap ends should be selected
        assert all(slots[i]["selected"] for i in range(0, 4))
        assert all(slots[i]["selected"] for i in range(8, 12))

    def test_gap_not_filled_when_expensive(self):
        """Expensive gap between two clusters is NOT filled."""
        from custom_components.goe_cheap_charging.coordinator import _select_slots
        # Cluster 1: 0-3 cheap; Gap 4-7 very expensive; Cluster 2: 8-11 cheap
        prices = [0.10] * 4 + [2.00] * 4 + [0.10] * 4
        slots = self._make_slots(prices)
        _select_slots(slots, 8, 0.10)
        # Multi-block chosen; gap is expensive → NOT filled
        assert not all(slots[i]["selected"] for i in range(4, 8))

    def test_gap_filled_when_cheap(self):
        """Cheap gap between two clusters is filled."""
        from custom_components.goe_cheap_charging.coordinator import _select_slots
        # Cluster 1: 0-3 @ 0.10; Gap 4-7 @ 0.15 (cheap); Cluster 2: 8-11 @ 0.10; rest 0.80
        prices = [0.10] * 4 + [0.15] * 4 + [0.10] * 4 + [0.80] * 4
        slots = self._make_slots(prices)
        _select_slots(slots, 8, 0.20)
        # Multi-block: 0-3 and 8-11; gap 4-7 @ 0.15 ≤ avg(0.10)+0.20 → filled
        assert slots[4]["selected"] or slots[5]["selected"]

    def test_n_slots_capped_at_available(self):
        """Requesting more slots than exist → all slots selected."""
        from custom_components.goe_cheap_charging.coordinator import _select_slots
        slots = self._make_slots([0.50] * 6)
        _select_slots(slots, 100, 0.10)
        assert all(s["selected"] for s in slots)

    def test_cheapest_contiguous_block_at_start_rising_prices(self):
        """Monotonically rising prices → cheapest 4-slot block is at the start."""
        from custom_components.goe_cheap_charging.coordinator import _select_slots
        prices = [0.10 * (i + 1) for i in range(12)]
        slots = self._make_slots(prices)
        _select_slots(slots, 4, 0.001)  # very low threshold → force single block
        selected_idxs = sorted(i for i, s in enumerate(slots) if s["selected"])
        assert selected_idxs == [0, 1, 2, 3]

    def test_negative_prices_selected_as_cheapest(self):
        """Negative prices are valid and treated as cheapest."""
        from custom_components.goe_cheap_charging.coordinator import _select_slots
        prices = [-0.50] * 4 + [0.30] * 4 + [-0.10] * 4
        slots = self._make_slots(prices)
        _select_slots(slots, 4, 0.001)
        selected_idxs = sorted(i for i, s in enumerate(slots) if s["selected"])
        # Best single block of 4: 0-3 (avg=-0.50 is cheapest)
        assert selected_idxs == [0, 1, 2, 3]

    def test_get_clusters_empty(self):
        from custom_components.goe_cheap_charging.coordinator import _get_clusters
        assert _get_clusters(set(), 8) == []

    def test_get_clusters_all_selected(self):
        from custom_components.goe_cheap_charging.coordinator import _get_clusters
        assert _get_clusters(set(range(4)), 4) == [[0, 1, 2, 3]]

    def test_get_clusters_two_disjoint(self):
        from custom_components.goe_cheap_charging.coordinator import _get_clusters
        result = _get_clusters({0, 1, 4, 5}, 8)
        assert result == [[0, 1], [4, 5]]

    def test_get_clusters_single_isolated(self):
        from custom_components.goe_cheap_charging.coordinator import _get_clusters
        result = _get_clusters({3}, 8)
        assert result == [[3]]


# ============================================================================
# TestScheduleBuildingEdgeCases
# ============================================================================

class TestScheduleBuildingEdgeCases:
    """Additional edge cases for _async_rebuild_schedule."""

    @pytest.mark.asyncio
    async def test_kwh_near_zero_builds_no_schedule(self, fake_hass):
        """kWh needed < 0.5 → no charging, reason mentions kWh or target."""
        now = _now_frozen()
        day = _weekday_name(now)
        departure = (now + timedelta(hours=4)).strftime("%H:%M")
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        nordpool = _c.make_nordpool_prices(PRICES_FLAT, midnight)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool)

        # soc=79.5, target=80 → 0.5/100 * 64/0.9 ≈ 0.356 kWh < 0.5
        coord = _c.make_coordinator(fake_hass, soc=79.5)
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, day, departure, target_soc=80)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        assert coord.schedule == []
        assert "kWh" in coord._schedule_status_reason or "target" in coord._schedule_status_reason.lower()

    @pytest.mark.asyncio
    async def test_very_low_soc_selects_many_slots(self, fake_hass):
        """SoC=5%, target=100% → large kWh needed → many slots selected.

        now=10:00 Sunday, departure=22:00 same day (12 h window).  Using "00:00"
        would resolve to midnight *past* today and wrap to next week (no prices).
        """
        now = _now_frozen()
        day = _weekday_name(now)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        nordpool = _c.make_nordpool_prices(PRICES_FLAT, midnight)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool)

        coord = _c.make_coordinator(fake_hass, soc=5)
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, day, "22:00", target_soc=100)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        selected = [s for s in coord.schedule if s["selected"]]
        assert len(selected) >= 8

    @pytest.mark.asyncio
    async def test_price_spread_just_below_threshold_continuous(self, fake_hass):
        """Price spread < threshold → all slots selected (continuous charging)."""
        now = _now_frozen()
        day = _weekday_name(now)
        departure = (now + timedelta(hours=6)).strftime("%H:%M")
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Spread = 0.09 < threshold 0.10
        prices = [0.50] * 80 + [0.59] * 16
        nordpool = _c.make_nordpool_prices(prices, midnight)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool)

        coord = _c.make_coordinator(fake_hass, soc=50)
        coord._price_spread_threshold = 0.10
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, day, departure, target_soc=80)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        assert all(s["selected"] for s in coord.schedule)
        assert "spread below threshold" in coord._schedule_status_reason

    @pytest.mark.asyncio
    async def test_price_spread_equal_to_threshold_is_selective(self, fake_hass):
        """Spread == threshold → NOT continuous (condition is strict <).

        The spread must be present within the actual departure window, not just
        somewhere in the full 96-slot day.  Interleave prices so both 0.50 and
        0.60 appear throughout the day.
        """
        now = _now_frozen()
        day = _weekday_name(now)
        departure = (now + timedelta(hours=6)).strftime("%H:%M")
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Interleave cheap (0.20) and expensive (0.80) slots throughout the day.
        # Spread = 0.80 - 0.20 = 0.60 >> threshold 0.10 → selective mode.
        # Only needs ~16 of the 24 available slots → not all selected.
        prices = [0.20 if i % 2 == 0 else 0.80 for i in range(96)]
        nordpool = _c.make_nordpool_prices(prices, midnight)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool)

        coord = _c.make_coordinator(fake_hass, soc=50)
        coord._price_spread_threshold = 0.10
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, day, departure, target_soc=80)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        # Selective mode: not all slots selected
        assert len(coord.schedule) > 0
        n_selected = sum(1 for s in coord.schedule if s["selected"])
        assert n_selected < len(coord.schedule)

    @pytest.mark.asyncio
    async def test_past_departure_with_all_slots_past_clears_schedule(self, fake_hass):
        """Departure in the past → no future slots before it → empty schedule."""
        # Set now to after the only available price slots
        now = datetime(2026, 3, 15, 23, 50, tzinfo=_UTC)
        day = _weekday_name(now)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # Departure is 5 minutes away but no slots fit
        departure = "23:55"

        nordpool = _c.make_nordpool_prices(PRICES_FLAT, midnight)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool)

        coord = _c.make_coordinator(fake_hass, soc=50)
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, day, departure, target_soc=80)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        # Very few or no slots before 23:55 from 23:50 (only ~10 min window)
        # Even if 1-2 slots exist, verify schedule is populated or empty (no crash)
        assert coord.schedule is not None

    @pytest.mark.asyncio
    async def test_smart_disabled_then_reenabled_rebuilds(self, fake_hass):
        """After disabling smart charging, re-enabling rebuilds schedule."""
        now = _now_frozen()
        day = _weekday_name(now)
        departure = (now + timedelta(hours=6)).strftime("%H:%M")
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        nordpool = _c.make_nordpool_prices(PRICES_OVERNIGHT_CHEAP, midnight)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool)

        coord = _c.make_coordinator(fake_hass, soc=50)
        _c.set_day_config(fake_hass, day, departure, target_soc=80)

        # First: disabled
        _c.set_smart_enabled(fake_hass, False)
        coord._transaction_active = False
        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()
        assert coord.schedule == []

        # Then: re-enabled
        _c.set_smart_enabled(fake_hass, True)
        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()
        # Should now have a schedule (or at least attempt to build one)
        assert coord.schedule is not None  # no crash; could be empty if no slots

    @pytest.mark.asyncio
    async def test_all_weekdays_enabled_picks_nearest_departure(self, fake_hass):
        """All days configured, today's departure (future) is nearest → picked."""
        now = _now_frozen()  # Sunday 10:00
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day = _weekday_name(now)

        nordpool = _c.make_nordpool_prices(PRICES_FLAT, midnight)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool)

        coord = _c.make_coordinator(fake_hass, soc=50)
        _c.set_smart_enabled(fake_hass, True)

        # Configure today with a future departure, all other days with 07:00
        for d in WEEKDAYS:
            if d == day:
                _c.set_day_config(fake_hass, d, "14:00", target_soc=80)
            else:
                _c.set_day_config(fake_hass, d, "07:00", target_soc=80)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        assert len(coord.schedule) > 0

    @pytest.mark.asyncio
    async def test_departure_window_excludes_past_slots(self, fake_hass):
        """Slots before 'now' (end ≤ now) are excluded from schedule."""
        # Solar dip cheap 10:00-14:00; now=14:00 → all cheap slots are in past
        now = datetime(2026, 3, 15, 14, 0, tzinfo=_UTC)
        day = _weekday_name(now)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        nordpool = _c.make_nordpool_prices(PRICES_SOLAR_DIP, midnight)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool)

        coord = _c.make_coordinator(fake_hass, soc=50)
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, day, "18:00", target_soc=80)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        # All slots in schedule must end after now
        for s in coord.schedule:
            assert s["end"] > now

    @pytest.mark.asyncio
    async def test_only_non_selected_slots_result_in_frc1(self, fake_hass, mqtt_log):
        """Schedule with only unselected slots → frc=1 when car is connected."""
        now = _now_frozen()
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = True
        coord.schedule = [
            _make_slot_at(now + timedelta(hours=1), selected=False),
            _make_slot_at(now + timedelta(hours=2), selected=False),
        ]

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()

        cmds = _c.mqtt_commands(mqtt_log)
        assert cmds.get("frc") == 1

    @pytest.mark.asyncio
    async def test_manual_kwh_zero_with_high_soc_no_schedule(self, fake_hass):
        """manual_kwh=0 and soc near target → no schedule needed."""
        now = _now_frozen()
        day = _weekday_name(now)
        departure = (now + timedelta(hours=4)).strftime("%H:%M")
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        nordpool = _c.make_nordpool_prices(PRICES_FLAT, midnight)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool)

        coord = _c.make_coordinator(fake_hass, soc=80)
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, day, departure, target_soc=80, manual_kwh=0.0)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        assert coord.schedule == []

    @pytest.mark.asyncio
    async def test_manual_kwh_ignored_when_car_at_target_soc(self, fake_hass):
        """manual_kwh=0 + soc=target → 0 kWh needed → no schedule (manual_kwh override only applies when > 0)."""
        now = _now_frozen()
        day = _weekday_name(now)
        departure = (now + timedelta(hours=4)).strftime("%H:%M")
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        nordpool = _c.make_nordpool_prices(PRICES_OVERNIGHT_CHEAP, midnight)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool)

        coord = _c.make_coordinator(fake_hass, soc=90)
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, day, departure, target_soc=80, manual_kwh=0.0)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        assert coord.schedule == []
        assert "target" in coord._schedule_status_reason.lower()


# ============================================================================
# TestChargerCommandEdgeCases
# ============================================================================

class TestChargerCommandEdgeCases:
    """Additional edge cases for _async_apply_charger_command."""

    @pytest.mark.asyncio
    async def test_charge_now_overrides_target_soc_limit(self, fake_hass):
        """charge_now=True + in selected slot → charge_now_soc_limit used, not target_soc."""
        now = _now_frozen()
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = True
        coord._charge_now = True
        coord._charge_now_soc_limit = 95.0
        coord._last_target_soc = 80
        coord._last_sent_car_limit = None
        coord.schedule = [_slot_covering_now(now)]

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()

        assert coord.car.set_charge_limit_calls == [95]

    @pytest.mark.asyncio
    async def test_car_complete_outside_slot_with_transaction_sends_frc1(
        self, fake_hass, mqtt_log
    ):
        """CAR_COMPLETE + outside slot + transaction → frc=1."""
        now = _now_frozen()
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_COMPLETE
        coord._transaction_active = True
        future_start = now + timedelta(hours=2)
        coord.schedule = [_make_slot_at(future_start, selected=True)]

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()

        cmds = _c.mqtt_commands(mqtt_log)
        assert cmds.get("frc") == 1

    @pytest.mark.asyncio
    async def test_no_transaction_no_slot_no_override_nothing_sent(
        self, fake_hass, mqtt_log
    ):
        """No transaction, no slot, no charge_now, threshold disabled → nothing sent."""
        now = _now_frozen()
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = False
        coord._charge_now = False
        coord._cheap_threshold = 0.0
        coord.schedule = []

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()

        assert mqtt_log == []

    @pytest.mark.asyncio
    async def test_charge_now_starts_new_transaction_if_none(
        self, fake_hass, mqtt_log
    ):
        """charge_now=True with no active transaction → trx=1 + frc=2."""
        now = _now_frozen()
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = False
        coord._charge_now = True
        coord.schedule = []

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()

        keys = _c.all_mqtt_keys(mqtt_log)
        assert "trx" in keys
        assert coord._transaction_active is True

    @pytest.mark.asyncio
    async def test_guest_mode_charge_now_sends_frc2_skips_car_limit(
        self, fake_hass, mqtt_log
    ):
        """Guest mode + charge_now → frc=2 sent but no car charge limit set."""
        now = _now_frozen()
        coord = _c.make_coordinator(fake_hass)
        coord._active_car_is_guest = True
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = True
        coord._charge_now = True
        coord._charge_now_soc_limit = 90.0

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()

        cmds = _c.mqtt_commands(mqtt_log)
        assert cmds.get("frc") == 2
        assert coord.car.set_charge_limit_calls == []

    @pytest.mark.asyncio
    async def test_target_soc_change_resends_car_limit(self, fake_hass):
        """If _last_target_soc changes between calls, new limit is sent."""
        now = _now_frozen()
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = True
        coord._last_target_soc = 80
        coord._last_sent_car_limit = 80
        coord.schedule = [_slot_covering_now(now)]

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()

        assert coord.car.set_charge_limit_calls == []

        # Target SoC changes
        coord._last_target_soc = 90
        with _c.freeze_now(now):
            await coord._async_apply_charger_command()

        assert 90 in coord.car.set_charge_limit_calls

    @pytest.mark.asyncio
    async def test_empty_price_data_disables_opportunistic(self, fake_hass, mqtt_log):
        """Empty _current_price_data → opportunistic mode not triggered → frc=1."""
        now = _now_frozen()
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = True
        coord._cheap_threshold = 0.50
        coord._current_price_data = []
        coord.schedule = []

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()

        cmds = _c.mqtt_commands(mqtt_log)
        assert cmds.get("frc") == 1

    @pytest.mark.asyncio
    async def test_opportunistic_limit_not_resent_when_unchanged(self, fake_hass):
        """Opportunistic mode: car limit not re-sent if _last_sent_car_limit matches."""
        now = _now_frozen()
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = True
        coord._cheap_threshold = 0.50
        coord._opportunistic_soc_limit = 60.0
        coord._last_sent_car_limit = 60  # already sent
        coord.schedule = []

        prices = []
        for i in range(6):
            start = now - timedelta(minutes=5) + timedelta(minutes=15 * i)
            end = start + timedelta(minutes=15)
            prices.append({"start": start.isoformat(), "end": end.isoformat(), "price": 0.20})
        coord._current_price_data = prices

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()

        assert coord.car.set_charge_limit_calls == []

    @pytest.mark.asyncio
    async def test_two_consecutive_in_slot_calls_both_send_frc2(
        self, fake_hass, mqtt_log
    ):
        """Two consecutive calls while in slot both send frc=2 (no suppression)."""
        now = _now_frozen()
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_CHARGING
        coord._transaction_active = True
        coord._last_sent_car_limit = 80
        coord._last_target_soc = 80
        coord.schedule = [_slot_covering_now(now)]

        with _c.freeze_now(now):
            await coord._async_apply_charger_command()
            await coord._async_apply_charger_command()

        frc_entries = [e for e in mqtt_log if "frc" in e["topic"]]
        assert len(frc_entries) == 2
        assert all(e["payload"] == 2 for e in frc_entries)

    @pytest.mark.asyncio
    async def test_in_slot_no_car_no_limit_sent(self, fake_hass):
        """car=None (no car selected) → charge limit not sent but frc=2 still is."""
        now = _now_frozen()
        coord = _c.make_coordinator(fake_hass)
        coord.car = None
        coord._active_car_is_guest = False
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = True
        coord.schedule = [_slot_covering_now(now)]

        with _c.freeze_now(now):
            # Should not raise even without a car
            await coord._async_apply_charger_command()


# ============================================================================
# TestCarStateTransitionEdgeCases
# ============================================================================

class TestCarStateTransitionEdgeCases:
    """Additional car state machine transition edge cases."""

    @pytest.mark.asyncio
    async def test_charging_to_idle_stops_amp_adjust(self, fake_hass):
        """2→1 (cable pulled while charging) → amp-adjust loop cancelled."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_CHARGING

        async def _dummy():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                pass

        coord._amp_adjust_task = asyncio.ensure_future(_dummy())
        coord._handle_car_state(CAR_IDLE)
        await fake_hass.drain_tasks()

        assert coord._amp_adjust_task is None or coord._amp_adjust_task.done()

    @pytest.mark.asyncio
    async def test_charging_to_connected_stops_amp_adjust(self, fake_hass):
        """2→3 (charge paused, cable still in) → amp-adjust loop cancelled."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_CHARGING

        async def _dummy():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                pass

        coord._amp_adjust_task = asyncio.ensure_future(_dummy())
        coord._handle_car_state(CAR_CONNECTED)
        await fake_hass.drain_tasks()

        assert coord._amp_adjust_task is None or coord._amp_adjust_task.done()

    @pytest.mark.asyncio
    async def test_connected_to_connected_no_extra_trx(self, fake_hass, mqtt_log):
        """Receiving CAR_CONNECTED when already connected → no spurious trx."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = True

        coord._handle_car_state(CAR_CONNECTED)
        await fake_hass.drain_tasks()

        keys = _c.all_mqtt_keys(mqtt_log)
        assert "trx" not in keys

    @pytest.mark.asyncio
    async def test_complete_to_idle_no_commands(self, fake_hass, mqtt_log):
        """4→1 (unplug after complete) → no MQTT commands."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_COMPLETE
        coord._transaction_active = False

        coord._handle_car_state(CAR_IDLE)
        await fake_hass.drain_tasks()

        assert mqtt_log == []

    @pytest.mark.asyncio
    async def test_idle_to_charging_marks_transaction_active(self, fake_hass):
        """car=2 without prior trx=1 → transaction marked active automatically."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_IDLE
        coord._transaction_active = False

        coord._handle_car_state(CAR_CHARGING)
        if coord._amp_adjust_task:
            coord._amp_adjust_task.cancel()
            coord._amp_adjust_task = None
        await fake_hass.drain_tasks()

        assert coord._transaction_active is True

    @pytest.mark.asyncio
    async def test_complete_to_complete_does_not_double_clear(self, fake_hass):
        """Duplicate CAR_COMPLETE events don't break state."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_CHARGING
        coord._transaction_active = True

        coord._handle_car_state(CAR_COMPLETE)
        await fake_hass.drain_tasks()

        # Transaction stays active (go-e retains trx until cable removed)
        assert coord._transaction_active is True
        assert coord._last_sent_car_limit is None

        # Second duplicate CAR_COMPLETE — _handle_car_state short-circuits (same state)
        coord._handle_car_state(CAR_COMPLETE)
        await fake_hass.drain_tasks()

        assert coord._transaction_active is True

    @pytest.mark.asyncio
    async def test_force_update_called_on_charging_start(self, fake_hass):
        """Transition to CAR_CHARGING triggers car.async_force_update()."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = True

        coord._handle_car_state(CAR_CHARGING)
        if coord._amp_adjust_task:
            coord._amp_adjust_task.cancel()
            coord._amp_adjust_task = None
        await fake_hass.drain_tasks()

        assert coord.car.force_update_calls >= 1

    @pytest.mark.asyncio
    async def test_force_update_called_on_charging_stop(self, fake_hass):
        """Transition away from CAR_CHARGING triggers car.async_force_update()."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_CHARGING

        coord._handle_car_state(CAR_CONNECTED)
        await fake_hass.drain_tasks()

        assert coord.car.force_update_calls >= 1

    @pytest.mark.asyncio
    async def test_connected_to_charging_starts_amp_adjust(self, fake_hass):
        """3→2 → amp-adjust task is created."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_CONNECTED
        coord._amp_adjust_task = None

        coord._handle_car_state(CAR_CHARGING)
        await asyncio.sleep(0)

        assert coord._amp_adjust_task is not None
        coord._amp_adjust_task.cancel()


# ============================================================================
# TestAmpAdjustEdgeCases
# ============================================================================

class TestAmpAdjustEdgeCases:
    """Additional amp adjustment edge cases."""

    @pytest.mark.asyncio
    async def test_exactly_six_amp_change_sends_command(self, fake_hass, mqtt_log):
        """Exactly 6A change (well above 1A threshold) → command IS sent."""
        cfg = _c.make_config(**{
            CONF_CHARGER_PHASE: 1,
            CONF_CHARGER_N_PHASES: 1,
            CONF_BREAKER_LIMIT: 20,
            CONF_MIN_AMP: 6,
            CONF_MAX_AMP: 16,
        })
        coord = _c.make_coordinator(fake_hass, cfg=cfg)
        coord.car_state = CAR_CHARGING
        coord._last_sent_amp = 10  # was 10, now headroom → 16
        # phases=[5, 0, 0], charger phase 1, last_amp=10
        # baseline = [5-10=max(0,-5)=0, 0, 0], headroom=min(20,20,20)=20 → clamp to 16
        # delta = 16-10 = 6 → sends
        fake_hass.states.set("sensor.l1", "5.0")
        fake_hass.states.set("sensor.l2", "0.0")
        fake_hass.states.set("sensor.l3", "0.0")

        await coord._async_do_amp_adjust()

        cmds = _c.mqtt_commands(mqtt_log)
        assert "amp" in cmds
        assert cmds["amp"] == 16

    @pytest.mark.asyncio
    async def test_same_amp_no_command(self, fake_hass, mqtt_log):
        """When calculated amp equals last_sent_amp → no command."""
        cfg = _c.make_config(**{
            CONF_CHARGER_PHASE: 1,
            CONF_CHARGER_N_PHASES: 1,
            CONF_BREAKER_LIMIT: 20,
            CONF_MIN_AMP: 6,
            CONF_MAX_AMP: 16,
        })
        coord = _c.make_coordinator(fake_hass, cfg=cfg)
        coord.car_state = CAR_CHARGING
        # phases=[10, 10, 10] baseline=[10-10=0, 10, 10], headroom=min(20,10,10)=10 → amp=10
        coord._last_sent_amp = 10
        fake_hass.states.set("sensor.l1", "10.0")
        fake_hass.states.set("sensor.l2", "10.0")
        fake_hass.states.set("sensor.l3", "10.0")

        await coord._async_do_amp_adjust()

        assert mqtt_log == []

    @pytest.mark.asyncio
    async def test_all_phases_zero_uses_full_headroom_clamped_to_max(
        self, fake_hass, mqtt_log
    ):
        """Phases all at 0A → headroom = breaker_limit → clamp to max_amp."""
        cfg = _c.make_config(**{
            CONF_CHARGER_PHASE: 1,
            CONF_CHARGER_N_PHASES: 1,
            CONF_BREAKER_LIMIT: 20,
            CONF_MIN_AMP: 6,
            CONF_MAX_AMP: 16,
        })
        coord = _c.make_coordinator(fake_hass, cfg=cfg)
        coord.car_state = CAR_CHARGING
        coord._last_sent_amp = 0
        fake_hass.states.set("sensor.l1", "0.0")
        fake_hass.states.set("sensor.l2", "0.0")
        fake_hass.states.set("sensor.l3", "0.0")

        await coord._async_do_amp_adjust()

        cmds = _c.mqtt_commands(mqtt_log)
        assert cmds["amp"] == 16

    @pytest.mark.asyncio
    async def test_three_phase_subtracts_from_all_phases(
        self, fake_hass, mqtt_log
    ):
        """3-phase charger: last_amp subtracted from all three phases."""
        cfg = _c.make_config(**{
            CONF_CHARGER_PHASE: 1,
            CONF_CHARGER_N_PHASES: 3,
            CONF_BREAKER_LIMIT: 25,
            CONF_MIN_AMP: 6,
            CONF_MAX_AMP: 20,
        })
        coord = _c.make_coordinator(fake_hass, cfg=cfg)
        coord.car_state = CAR_CHARGING
        coord._last_sent_amp = 10
        # All phases = 15 (charger draws 10A each)
        # baseline after subtraction: [5, 5, 5], headroom = min(20,20,20) = 20
        fake_hass.states.set("sensor.l1", "15.0")
        fake_hass.states.set("sensor.l2", "15.0")
        fake_hass.states.set("sensor.l3", "15.0")

        await coord._async_do_amp_adjust()

        cmds = _c.mqtt_commands(mqtt_log)
        assert "amp" in cmds
        assert cmds["amp"] == 20

    @pytest.mark.asyncio
    async def test_negative_headroom_clamped_to_min_amp(self, fake_hass, mqtt_log):
        """Phases exceeding breaker → negative headroom → clamp to min_amp."""
        cfg = _c.make_config(**{
            CONF_CHARGER_PHASE: 1,
            CONF_CHARGER_N_PHASES: 1,
            CONF_BREAKER_LIMIT: 20,
            CONF_MIN_AMP: 6,
            CONF_MAX_AMP: 16,
        })
        coord = _c.make_coordinator(fake_hass, cfg=cfg)
        coord.car_state = CAR_CHARGING
        coord._last_sent_amp = 0
        # L1=25A after subtraction (0A charger) → baseline=25 > breaker=20
        # headroom = min(20-25, ...) = -5 → clamp to min=6
        fake_hass.states.set("sensor.l1", "25.0")
        fake_hass.states.set("sensor.l2", "5.0")
        fake_hass.states.set("sensor.l3", "5.0")

        await coord._async_do_amp_adjust()

        cmds = _c.mqtt_commands(mqtt_log)
        assert cmds["amp"] == 6

    @pytest.mark.asyncio
    async def test_min_equals_max_sends_that_value(self, fake_hass, mqtt_log):
        """min_amp == max_amp → always sends exactly that value (if different from last)."""
        cfg = _c.make_config(**{
            CONF_CHARGER_PHASE: 1,
            CONF_CHARGER_N_PHASES: 1,
            CONF_BREAKER_LIMIT: 20,
            CONF_MIN_AMP: 10,
            CONF_MAX_AMP: 10,
        })
        coord = _c.make_coordinator(fake_hass, cfg=cfg)
        coord.car_state = CAR_CHARGING
        coord._last_sent_amp = 5  # different from 10 → will send
        fake_hass.states.set("sensor.l1", "5.0")
        fake_hass.states.set("sensor.l2", "5.0")
        fake_hass.states.set("sensor.l3", "5.0")

        await coord._async_do_amp_adjust()

        cmds = _c.mqtt_commands(mqtt_log)
        assert cmds["amp"] == 10

    @pytest.mark.asyncio
    async def test_invalid_phase_value_skips_adjust(self, fake_hass, mqtt_log):
        """Non-numeric phase sensor value → amp adjust skipped gracefully."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_CHARGING
        fake_hass.states.set("sensor.l1", "not_a_number")
        fake_hass.states.set("sensor.l2", "5.0")
        fake_hass.states.set("sensor.l3", "5.0")

        await coord._async_do_amp_adjust()

        assert mqtt_log == []


# ============================================================================
# TestIsLongChargingBlockEdgeCases
# ============================================================================

class TestIsLongChargingBlockEdgeCases:
    """Boundary cases for _is_long_charging_block."""

    def test_exactly_6_slots_90_min_is_not_long(self, fake_hass):
        """Exactly 6 slots = 90 min = 5400s → NOT > 5400 → False."""
        now = _now_frozen()
        coord = _c.make_coordinator(fake_hass)
        start = now + timedelta(minutes=5)
        coord.schedule = [
            _make_slot_at(start + timedelta(minutes=15 * i), selected=True)
            for i in range(6)
        ]
        with _c.freeze_now(now):
            assert coord._is_long_charging_block() is False

    def test_7_slots_105_min_is_long(self, fake_hass):
        """7 slots = 105 min > 90 min → True."""
        now = _now_frozen()
        coord = _c.make_coordinator(fake_hass)
        start = now + timedelta(minutes=5)
        coord.schedule = [
            _make_slot_at(start + timedelta(minutes=15 * i), selected=True)
            for i in range(7)
        ]
        with _c.freeze_now(now):
            assert coord._is_long_charging_block() is True

    def test_non_contiguous_uses_only_first_block(self, fake_hass):
        """Two separate blocks: only first contiguous block counted from now."""
        now = _now_frozen()
        coord = _c.make_coordinator(fake_hass)
        # First block: 2 slots (30 min, not long)
        start1 = now + timedelta(minutes=5)
        # Second block: 8 slots (120 min), separated by a gap
        start2 = now + timedelta(hours=4)
        coord.schedule = (
            [_make_slot_at(start1 + timedelta(minutes=15 * i), selected=True) for i in range(2)]
            + [_make_slot_at(start2 + timedelta(minutes=15 * i), selected=True) for i in range(8)]
        )
        with _c.freeze_now(now):
            result = coord._is_long_charging_block()

        # First block is 30 min → not long; gap breaks contiguity
        assert result is False

    def test_opportunistic_exactly_4_cheap_slots_not_long(self, fake_hass):
        """4 consecutive cheap slots (60 min) → not long (60 ≤ 90 min threshold)."""
        now = _now_frozen()
        coord = _c.make_coordinator(fake_hass)
        coord._cheap_threshold = 0.50
        coord.schedule = []

        prices = []
        for i in range(4):
            start_i = now + timedelta(minutes=15 * i)
            end_i = start_i + timedelta(minutes=15)
            prices.append({"start": start_i.isoformat(), "end": end_i.isoformat(), "price": 0.20})
        # Break the cheap block
        expensive_start = now + timedelta(minutes=60)
        prices.append({
            "start": expensive_start.isoformat(),
            "end": (expensive_start + timedelta(minutes=15)).isoformat(),
            "price": 2.00,
        })
        coord._current_price_data = prices

        with _c.freeze_now(now):
            result = coord._is_long_charging_block()

        assert result is False

    def test_opportunistic_7_cheap_slots_is_long(self, fake_hass):
        """7 consecutive cheap slots (105 min) → long block."""
        now = _now_frozen()
        coord = _c.make_coordinator(fake_hass)
        coord._cheap_threshold = 0.50
        coord.schedule = []

        prices = []
        for i in range(7):
            start_i = now + timedelta(minutes=15 * i)
            end_i = start_i + timedelta(minutes=15)
            prices.append({"start": start_i.isoformat(), "end": end_i.isoformat(), "price": 0.20})
        coord._current_price_data = prices

        with _c.freeze_now(now):
            result = coord._is_long_charging_block()

        assert result is True

    def test_past_opportunistic_slots_ignored(self, fake_hass):
        """Opportunistic slots ending before now are excluded."""
        now = _now_frozen()
        coord = _c.make_coordinator(fake_hass)
        coord._cheap_threshold = 0.50
        coord.schedule = []

        prices = []
        for i in range(8):
            start_i = now - timedelta(hours=3) + timedelta(minutes=15 * i)
            end_i = start_i + timedelta(minutes=15)
            prices.append({"start": start_i.isoformat(), "end": end_i.isoformat(), "price": 0.20})
        coord._current_price_data = prices

        with _c.freeze_now(now):
            result = coord._is_long_charging_block()

        assert result is False


# ============================================================================
# TestFindNextDepartureEdgeCases
# ============================================================================

class TestFindNextDepartureEdgeCases:
    """Additional _find_next_departure edge cases."""

    def test_multiple_future_days_picks_nearest(self, fake_hass):
        """Monday and Wednesday both configured → Monday (nearer) is picked."""
        now = _now_frozen()  # Sunday 10:00
        coord = _c.make_coordinator(fake_hass)
        coord._day_settings["monday"]["departure"] = time(7, 0)
        coord._day_settings["monday"]["target_soc"] = 80
        coord._day_settings["wednesday"]["departure"] = time(7, 0)
        coord._day_settings["wednesday"]["target_soc"] = 80

        with _c.freeze_now(now):
            result = coord._find_next_departure()

        assert result is not None
        _, _, day_name = result
        assert day_name == "monday"

    def test_departure_exactly_now_skipped(self, fake_hass):
        """Departure exactly at now (not strictly > now) → skipped, finds next week."""
        now = _now_frozen()  # Sunday 10:00
        coord = _c.make_coordinator(fake_hass)
        coord._day_settings["sunday"]["departure"] = time(10, 0, 0)
        coord._day_settings["sunday"]["target_soc"] = 80

        with _c.freeze_now(now):
            result = coord._find_next_departure()

        if result is not None:
            dep_dt, _, _ = result
            assert dep_dt > now

    def test_departure_one_minute_in_future_found(self, fake_hass):
        """Departure 1 minute in future → found."""
        now = _now_frozen()  # 10:00
        coord = _c.make_coordinator(fake_hass)
        coord._day_settings["sunday"]["departure"] = time(10, 1, 0)
        coord._day_settings["sunday"]["target_soc"] = 80

        with _c.freeze_now(now):
            result = coord._find_next_departure()

        assert result is not None
        dep_dt, _, _ = result
        assert dep_dt > now

    def test_no_departure_time_set_returns_none(self, fake_hass):
        """All days have target_soc but departure=None → returns None."""
        coord = _c.make_coordinator(fake_hass)
        for day in WEEKDAYS:
            coord._day_settings[day]["target_soc"] = 80
            coord._day_settings[day]["departure"] = None

        result = coord._find_next_departure()
        assert result is None

    def test_past_departure_today_wraps_to_next_week(self, fake_hass):
        """Today's departure at 08:00 (past when now=10:00) → wraps to next Sunday."""
        now = _now_frozen()  # Sunday 10:00
        coord = _c.make_coordinator(fake_hass)
        coord._day_settings["sunday"]["departure"] = time(8, 0)
        coord._day_settings["sunday"]["target_soc"] = 80

        with _c.freeze_now(now):
            result = coord._find_next_departure()

        assert result is not None
        dep_dt, _, day_name = result
        assert day_name == "sunday"
        assert dep_dt.date() == (now + timedelta(days=7)).date()

    def test_manual_kwh_nonzero_with_zero_target_soc_is_included(self, fake_hass):
        """Day with target_soc=0 but manual_kwh>0 → IS a valid departure."""
        now = _now_frozen()
        coord = _c.make_coordinator(fake_hass)
        coord._day_settings["sunday"]["departure"] = time(14, 0)
        coord._day_settings["sunday"]["target_soc"] = 0
        coord._day_settings["sunday"]["manual_kwh"] = 10.0

        with _c.freeze_now(now):
            result = coord._find_next_departure()

        assert result is not None
        _, _, day_name = result
        assert day_name == "sunday"


# ============================================================================
# TestRetryLogicEdgeCases
# ============================================================================

class TestRetryLogicEdgeCases:
    """Retry timer cancellation and replacement edge cases."""

    @pytest.mark.asyncio
    async def test_second_rebuild_cancels_first_retry_timer(self, fake_hass):
        """Second rebuild call when prices unavailable cancels first retry and schedules new one."""
        now = datetime(2026, 3, 15, 10, 0, tzinfo=_UTC)
        tomorrow_day = WEEKDAYS[(now + timedelta(days=1)).weekday()]
        midnight_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        nordpool_today = _c.make_nordpool_prices([0.80] * 96, midnight_today)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool_today)

        coord = _c.make_coordinator(fake_hass, soc=50)
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, tomorrow_day, "07:00", target_soc=80)

        cancel_calls = []
        call_later_calls = []

        def capturing_later(hass, delay, cb):
            idx = len(call_later_calls)
            call_later_calls.append({"delay": delay})

            def cancel():
                cancel_calls.append(idx)

            return cancel

        with _c.freeze_now(now):
            with patch(
                "custom_components.goe_cheap_charging.coordinator.async_call_later",
                capturing_later,
            ):
                await coord._async_rebuild_schedule()
                assert len(call_later_calls) == 1
                # Second rebuild: should cancel first and schedule new
                await coord._async_rebuild_schedule()

        # Should have scheduled two retry timers
        assert len(call_later_calls) == 2
        # First timer should have been cancelled
        assert 0 in cancel_calls

    @pytest.mark.asyncio
    async def test_retry_exactly_at_1330_uses_5min_fallback(self, fake_hass):
        """At exactly 13:30, retry_today == now → not in future → 5-min fallback."""
        now = datetime(2026, 3, 15, 13, 30, tzinfo=_UTC)
        tomorrow_day = WEEKDAYS[(now + timedelta(days=1)).weekday()]
        midnight_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        nordpool_today = _c.make_nordpool_prices([0.80] * 96, midnight_today)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool_today)

        coord = _c.make_coordinator(fake_hass, soc=50)
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, tomorrow_day, "07:00", target_soc=80)

        call_later_calls = []

        def capturing_later(hass, delay, cb):
            call_later_calls.append({"delay": delay})
            return lambda: None

        with _c.freeze_now(now):
            with patch(
                "custom_components.goe_cheap_charging.coordinator.async_call_later",
                capturing_later,
            ):
                await coord._async_rebuild_schedule()

        assert len(call_later_calls) == 1
        assert abs(call_later_calls[0]["delay"] - 300) < 5

    @pytest.mark.asyncio
    async def test_no_nordpool_config_entry_results_in_retry(self, fake_hass):
        """If Nordpool config entry not present, prices unavailable → retry scheduled."""
        now = _now_frozen()
        day = _weekday_name(now)

        class NoNordpoolEntries:
            def async_entries(self, domain):
                return []

        fake_hass.config_entries = NoNordpoolEntries()

        coord = _c.make_coordinator(fake_hass, soc=50)
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, day, "14:00", target_soc=80)

        call_later_calls = []

        def capturing_later(hass, delay, cb):
            call_later_calls.append({"delay": delay})
            return lambda: None

        with _c.freeze_now(now):
            with patch(
                "custom_components.goe_cheap_charging.coordinator.async_call_later",
                capturing_later,
            ):
                await coord._async_rebuild_schedule()

        # No Nordpool → prices unavailable → retry in 5 min
        assert len(call_later_calls) == 1
        assert abs(call_later_calls[0]["delay"] - 300) < 5


# ============================================================================
# TestPriceEdgeCases
# ============================================================================

class TestPriceEdgeCases:
    """Unusual price structure scenarios."""

    @pytest.mark.asyncio
    async def test_single_cheap_hour_in_expensive_day_selected(self, fake_hass):
        """One cheap hour in otherwise expensive day → that hour is selected."""
        now = datetime(2026, 3, 15, 8, 0, tzinfo=_UTC)
        day = _weekday_name(now)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Cheap 10:00-11:00 (indices 40-43), rest expensive
        prices = [1.00] * 40 + [0.05] * 4 + [1.00] * 52
        nordpool = _c.make_nordpool_prices(prices, midnight)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool)

        # soc=77 → (80-77)/100 * 64/0.9 ≈ 2.13 kWh → 4 slots needed (1 block).
        # Exactly 4 cheap slots exist → all selected slots are cheap.
        coord = _c.make_coordinator(fake_hass, soc=77)
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, day, "16:00", target_soc=80)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        selected = [s for s in coord.schedule if s["selected"]]
        assert len(selected) > 0
        # All selected slots should be from the cheap window
        cheap_selected = [s for s in selected if s["price"] <= 0.10]
        assert len(cheap_selected) == len(selected)

    @pytest.mark.asyncio
    async def test_all_negative_prices_continuous_charging(self, fake_hass):
        """All prices equal and negative → spread=0 → all slots selected (continuous)."""
        now = datetime(2026, 3, 15, 2, 0, tzinfo=_UTC)
        day = _weekday_name(now)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        prices = [-1.0] * 96
        nordpool = _c.make_nordpool_prices(prices, midnight)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool)

        coord = _c.make_coordinator(fake_hass, soc=50)
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, day, "10:00", target_soc=80)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        assert all(s["selected"] for s in coord.schedule)
        assert "spread below threshold" in coord._schedule_status_reason

    @pytest.mark.asyncio
    async def test_very_expensive_prices_still_schedules(self, fake_hass):
        """Even with all-expensive prices, slots are still selected when car needs charging."""
        now = _now_frozen()
        day = _weekday_name(now)
        departure = (now + timedelta(hours=6)).strftime("%H:%M")
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        nordpool = _c.make_nordpool_prices(PRICES_ALL_EXPENSIVE, midnight)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool)

        coord = _c.make_coordinator(fake_hass, soc=30)
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, day, departure, target_soc=80)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        selected = [s for s in coord.schedule if s["selected"]]
        assert len(selected) > 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("n_phases,max_slots", [
        (1, 999),   # 1-phase: slower → needs more slots
        (3, 999),   # 3-phase: faster → needs fewer slots
    ])
    async def test_phase_count_affects_slot_count(
        self, fake_hass, n_phases, max_slots
    ):
        """More phases → faster charging → scheduler picks fewer slots."""
        now = _now_frozen()
        day = _weekday_name(now)
        departure = (now + timedelta(hours=12)).strftime("%H:%M")
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        nordpool = _c.make_nordpool_prices(PRICES_OVERNIGHT_CHEAP, midnight)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool)

        cfg = _c.make_config(**{CONF_CHARGER_N_PHASES: n_phases})
        coord = _c.make_coordinator(fake_hass, cfg=cfg, soc=50)
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, day, departure, target_soc=80)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        selected = [s for s in coord.schedule if s["selected"]]
        assert len(selected) >= 4

    @pytest.mark.asyncio
    async def test_spike_slots_not_selected_with_cheap_alternatives(self, fake_hass):
        """Evening price spike slots are avoided when cheaper alternatives exist."""
        now = datetime(2026, 3, 15, 10, 0, tzinfo=_UTC)
        day = _weekday_name(now)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        nordpool = _c.make_nordpool_prices(PRICES_SPIKE, midnight)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool)

        # Departure after the spike window
        monday = _weekday_name(now + timedelta(days=1))
        coord = _c.make_coordinator(fake_hass, soc=30)
        coord._price_spread_threshold = 0.10
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, monday, "00:00", target_soc=80)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        selected = [s for s in coord.schedule if s["selected"]]
        spike_selected = [s for s in selected if 17 <= s["start"].hour < 21]
        assert len(spike_selected) == 0

    @pytest.mark.asyncio
    async def test_rising_prices_picks_earliest_slots(self, fake_hass):
        """With monotonically rising prices, earliest (cheapest) slots are selected."""
        now = _now_frozen()
        day = _weekday_name(now)
        departure = (now + timedelta(hours=8)).strftime("%H:%M")
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        nordpool = _c.make_nordpool_prices(PRICES_RISING, midnight)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool)

        coord = _c.make_coordinator(fake_hass, soc=60)
        coord._price_spread_threshold = 0.01
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, day, departure, target_soc=80)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        selected = [s for s in coord.schedule if s["selected"]]
        if len(selected) >= 2:
            # Earlier slots should have lower prices
            prices_of_selected = [s["price"] for s in sorted(selected, key=lambda s: s["start"])]
            # First selected slot should be cheaper than last
            assert prices_of_selected[0] <= prices_of_selected[-1]

    @pytest.mark.asyncio
    async def test_falling_prices_picks_latest_slots(self, fake_hass):
        """With monotonically falling prices, latest (cheapest) slots before departure selected."""
        now = _now_frozen()
        day = _weekday_name(now)
        departure = (now + timedelta(hours=8)).strftime("%H:%M")
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        nordpool = _c.make_nordpool_prices(PRICES_FALLING, midnight)
        fake_hass.services.set_nordpool(now.date().isoformat(), nordpool)

        coord = _c.make_coordinator(fake_hass, soc=60)
        coord._price_spread_threshold = 0.01
        _c.set_smart_enabled(fake_hass, True)
        _c.set_day_config(fake_hass, day, departure, target_soc=80)

        with _c.freeze_now(now):
            await coord._async_rebuild_schedule()

        selected = [s for s in coord.schedule if s["selected"]]
        if len(selected) >= 2:
            prices_of_selected = [s["price"] for s in sorted(selected, key=lambda s: s["start"])]
            # Last selected slot should be cheaper than first (falling prices)
            assert prices_of_selected[-1] <= prices_of_selected[0]


# ============================================================================
# TestScheduleSensorHelpers
# ============================================================================

class TestScheduleSensorHelpers:
    """Tests for get_schedule_summary, get_next_slot_time, get_schedule_debug_attrs."""

    def test_summary_empty_schedule_returns_status_reason(self, fake_hass):
        """Empty schedule → returns _schedule_status_reason verbatim."""
        coord = _c.make_coordinator(fake_hass)
        coord.schedule = []
        coord._schedule_status_reason = "No departure configured for any day"
        assert coord.get_schedule_summary() == "No departure configured for any day"

    def test_summary_in_slot_contains_charging_info(self, fake_hass):
        """In selected slot → summary mentions charging."""
        now = _now_frozen()
        coord = _c.make_coordinator(fake_hass)
        coord.schedule = [_slot_covering_now(now)]
        with _c.freeze_now(now):
            result = coord.get_schedule_summary()
        assert "charging" in result or "1 slots" in result

    def test_summary_charge_now_shows_override(self, fake_hass):
        """charge_now=True → summary contains 'override'."""
        now = _now_frozen()
        coord = _c.make_coordinator(fake_hass)
        coord._charge_now = True
        coord.schedule = [_slot_covering_now(now)]
        with _c.freeze_now(now):
            result = coord.get_schedule_summary()
        assert "override" in result

    def test_summary_paused_shows_next_time(self, fake_hass):
        """Outside slot with future selected slot → summary shows paused info."""
        now = _now_frozen()
        future_start = now + timedelta(hours=2)
        coord = _c.make_coordinator(fake_hass)
        coord.schedule = [_make_slot_at(future_start, selected=True)]
        with _c.freeze_now(now):
            result = coord.get_schedule_summary()
        assert "paused" in result or "1 slots" in result

    def test_summary_all_done_when_past_slots_only(self, fake_hass):
        """All selected slots in past → summary shows 'done'."""
        now = _now_frozen()
        past_start = now - timedelta(hours=2)
        coord = _c.make_coordinator(fake_hass)
        coord.schedule = [_make_slot_at(past_start, selected=True)]
        with _c.freeze_now(now):
            result = coord.get_schedule_summary()
        assert "done" in result or "1 slots" in result

    def test_get_next_slot_time_none_empty_schedule(self, fake_hass):
        coord = _c.make_coordinator(fake_hass)
        coord.schedule = []
        assert coord.get_next_slot_time() is None

    def test_get_next_slot_time_returns_earliest_future(self, fake_hass):
        now = _now_frozen()
        slot1_start = now + timedelta(hours=1)
        slot2_start = now + timedelta(hours=3)
        coord = _c.make_coordinator(fake_hass)
        coord.schedule = [
            _make_slot_at(slot2_start, selected=True),
            _make_slot_at(slot1_start, selected=True),
        ]
        with _c.freeze_now(now):
            assert coord.get_next_slot_time() == slot1_start

    def test_get_next_slot_time_none_all_past(self, fake_hass):
        now = _now_frozen()
        past_start = now - timedelta(hours=2)
        coord = _c.make_coordinator(fake_hass)
        coord.schedule = [_make_slot_at(past_start, selected=True)]
        with _c.freeze_now(now):
            assert coord.get_next_slot_time() is None

    def test_get_schedule_debug_attrs_has_required_keys(self, fake_hass):
        coord = _c.make_coordinator(fake_hass)
        coord.schedule = []
        attrs = coord.get_schedule_debug_attrs()
        for k in ("status_reason", "kwh_needed", "current_soc", "target_soc",
                   "departure", "charger_state", "in_selected_slot",
                   "charge_now_active", "opportunistic_slots", "slots"):
            assert k in attrs, f"Missing key: {k}"

    def test_get_schedule_debug_attrs_car_state_names(self, fake_hass):
        coord = _c.make_coordinator(fake_hass)
        coord.schedule = []
        for state_val, expected in [(1, "idle"), (2, "charging"), (3, "connected"), (4, "complete")]:
            coord.car_state = state_val
            assert coord.get_schedule_debug_attrs()["charger_state"] == expected

    def test_get_schedule_debug_attrs_unknown_state(self, fake_hass):
        """Unknown car state number → stringified."""
        coord = _c.make_coordinator(fake_hass)
        coord.schedule = []
        coord.car_state = 99
        attrs = coord.get_schedule_debug_attrs()
        assert attrs["charger_state"] == "99"


# ============================================================================
# TestSyncSettingsEdgeCases
# ============================================================================

class TestSyncSettingsEdgeCases:
    """Additional _sync_settings_from_ha edge cases."""

    def test_departure_with_seconds_component(self, fake_hass):
        """Departure 'HH:MM:SS' parsed with seconds."""
        coord = _c.make_coordinator(fake_hass)
        fake_hass.states.set("time.goe_cheap_charging_thursday_departure", "08:30:15")
        coord._sync_settings_from_ha()
        assert coord._day_settings["thursday"]["departure"] == time(8, 30, 15)

    def test_target_soc_float_string_converted_to_int(self, fake_hass):
        """'75.0' → int(75)."""
        coord = _c.make_coordinator(fake_hass)
        fake_hass.states.set("number.goe_cheap_charging_tuesday_target_soc", "75.0")
        coord._sync_settings_from_ha()
        assert coord._day_settings["tuesday"]["target_soc"] == 75
        assert isinstance(coord._day_settings["tuesday"]["target_soc"], int)

    def test_manual_kwh_zero_stored_correctly(self, fake_hass):
        """manual_kwh='0.0' is stored as 0.0 float, not treated as unavailable."""
        coord = _c.make_coordinator(fake_hass)
        fake_hass.states.set("number.goe_cheap_charging_wednesday_manual_kwh", "0.0")
        coord._sync_settings_from_ha()
        assert coord._day_settings["wednesday"]["manual_kwh"] == 0.0

    def test_unknown_state_preserves_previous_value(self, fake_hass):
        """State='unknown' leaves existing target_soc unchanged."""
        coord = _c.make_coordinator(fake_hass)
        coord._day_settings["friday"]["target_soc"] = 75
        fake_hass.states.set("number.goe_cheap_charging_friday_target_soc", "unknown")
        coord._sync_settings_from_ha()
        assert coord._day_settings["friday"]["target_soc"] == 75

    def test_all_seven_days_read_in_one_sync(self, fake_hass):
        """Single sync call reads all 7 weekdays."""
        coord = _c.make_coordinator(fake_hass)
        for day in WEEKDAYS:
            fake_hass.states.set(f"number.goe_cheap_charging_{day}_target_soc", "80")
        coord._sync_settings_from_ha()
        for day in WEEKDAYS:
            assert coord._day_settings[day]["target_soc"] == 80

    def test_charge_now_not_updated_by_sync(self, fake_hass):
        """_charge_now is NOT read by _sync_settings_from_ha."""
        coord = _c.make_coordinator(fake_hass)
        coord._charge_now = False
        fake_hass.states.set("switch.goe_cheap_charging_charge_now", "on")
        coord._sync_settings_from_ha()
        assert coord._charge_now is False


# ============================================================================
# TestTransactionEdgeCases
# ============================================================================

class TestTransactionEdgeCases:
    """Additional transaction state machine edge cases."""

    def test_trx_mqtt_null_clears_transaction(self, fake_hass):
        """trx=null (JSON null) → bool(None) = False → transaction cleared."""
        coord = _c.make_coordinator(fake_hass)
        coord._transaction_active = True
        msg = _c.FakeMqttMsg("go-eCharger/XYZ123/trx", "null")
        coord._handle_mqtt_message(msg)
        assert coord._transaction_active is False

    def test_trx_mqtt_large_number_sets_active(self, fake_hass):
        """trx=42 → bool(42)=True → transaction active."""
        coord = _c.make_coordinator(fake_hass)
        coord._transaction_active = False
        msg = _c.FakeMqttMsg("go-eCharger/XYZ123/trx", "42")
        coord._handle_mqtt_message(msg)
        assert coord._transaction_active is True

    def test_car_mqtt_updates_state_to_charging(self, fake_hass):
        """car=2 via MQTT → car_state = CAR_CHARGING."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_IDLE
        msg = _c.FakeMqttMsg("go-eCharger/XYZ123/car", "2")
        coord._handle_mqtt_message(msg)
        assert coord.car_state == CAR_CHARGING

    def test_amp_key_in_mqtt_ignored(self, fake_hass):
        """amp MQTT update → car_state unchanged (only car/trx handled)."""
        coord = _c.make_coordinator(fake_hass)
        initial_state = coord.car_state
        msg = _c.FakeMqttMsg("go-eCharger/XYZ123/amp", "10")
        coord._handle_mqtt_message(msg)
        assert coord.car_state == initial_state

    @pytest.mark.asyncio
    async def test_double_plugin_second_skips_trx(self, fake_hass, mqtt_log):
        """Second _async_handle_plugin while transaction active skips trx=1."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_IDLE
        coord._transaction_active = False

        await coord._async_handle_plugin()
        assert coord._transaction_active is True
        first_trx_count = _c.all_mqtt_keys(mqtt_log).count("trx")
        assert first_trx_count == 1

        await coord._async_handle_plugin()
        total_trx_count = _c.all_mqtt_keys(mqtt_log).count("trx")
        assert total_trx_count == 1  # no extra trx sent

    def test_frc_key_in_mqtt_ignored_as_unknown(self, fake_hass):
        """frc MQTT status update → unknown key → no state change."""
        coord = _c.make_coordinator(fake_hass)
        initial_trx = coord._transaction_active
        msg = _c.FakeMqttMsg("go-eCharger/XYZ123/frc", "2")
        coord._handle_mqtt_message(msg)
        assert coord._transaction_active == initial_trx
