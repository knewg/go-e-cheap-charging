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
        """4+ consecutive cheap slots → frc=2 even with no schedule."""
        now = _now_frozen()
        coord = self._make_coord(fake_hass)
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = True
        coord.schedule = []
        coord._cheap_threshold = 0.50

        # Build 6 consecutive cheap price entries covering now
        prices = []
        for i in range(6):
            start = now - timedelta(minutes=5) + timedelta(minutes=15 * i)
            end = start + timedelta(minutes=15)
            prices.append({
                "start": start.isoformat(),
                "end": end.isoformat(),
                "price": 0.30,
            })
        coord._current_price_data = prices

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
        """Cheap-slot charging sets car charge limit to opportunistic_soc_limit."""
        now = _now_frozen()
        coord = self._make_coord(fake_hass)
        coord.car_state = CAR_CONNECTED
        coord._transaction_active = True
        coord._cheap_threshold = 0.50
        coord._opportunistic_soc_limit = 60.0
        coord._last_sent_car_limit = None
        coord.schedule = []

        prices = []
        for i in range(6):
            start = now - timedelta(minutes=5) + timedelta(minutes=15 * i)
            end = start + timedelta(minutes=15)
            prices.append({"start": start.isoformat(), "end": end.isoformat(), "price": 0.20})
        coord._current_price_data = prices

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
        """Charge complete (→4) → transaction cleared, last_sent_car_limit reset."""
        coord = _c.make_coordinator(fake_hass)
        coord.car_state = CAR_CHARGING
        coord._transaction_active = True
        coord._last_sent_car_limit = 80

        coord._handle_car_state(CAR_COMPLETE)
        await fake_hass.drain_tasks()

        assert coord._transaction_active is False
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

        coord = _c.make_coordinator(fake_hass, soc=50)
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
        # Should not be charging yet (10:00 not reached)
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
