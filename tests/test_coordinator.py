"""Unit tests for the EV Smart Charging coordinator logic."""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Minimal stubs so tests run without a full HA install
import sys
import types

# Stub homeassistant packages
for mod in [
    "homeassistant",
    "homeassistant.core",
    "homeassistant.config_entries",
    "homeassistant.components",
    "homeassistant.components.mqtt",
    "homeassistant.helpers",
    "homeassistant.helpers.update_coordinator",
    "homeassistant.helpers.event",
    "homeassistant.helpers.restore_state",
    "homeassistant.util",
    "homeassistant.util.dt",
]:
    if mod not in sys.modules:
        sys.modules[mod] = types.ModuleType(mod)

# Provide minimal DataUpdateCoordinator stub
hass_helpers_update = sys.modules["homeassistant.helpers.update_coordinator"]
hass_helpers_update.DataUpdateCoordinator = object

# Provide dt_util stubs
import homeassistant.util.dt as dt_util_stub
_UTC = timezone.utc

def _now():
    return datetime.now(_UTC)

dt_util_stub.now = _now
dt_util_stub.as_local = lambda dt: dt
dt_util_stub.parse_datetime = lambda s: datetime.fromisoformat(s)
dt_util_stub.DEFAULT_TIME_ZONE = _UTC


# -----------------------------------------------------------------------
# Test helpers
# -----------------------------------------------------------------------

def _make_slot(start_iso: str, price: float, selected: bool = False) -> dict:
    start = datetime.fromisoformat(start_iso)
    end = start + timedelta(minutes=15)
    return {"start": start, "end": end, "price": price, "selected": selected}


# -----------------------------------------------------------------------
# Schedule selection logic (pure, no HA dependencies)
# -----------------------------------------------------------------------

def select_cheapest_slots(slots: list[dict], slots_needed: int) -> list[dict]:
    """Copy of coordinator slot-selection logic, extracted for testing."""
    import copy
    slots = copy.deepcopy(slots)
    sorted_by_price = sorted(slots, key=lambda s: s["price"])
    cheap_starts = {s["start"] for s in sorted_by_price[:slots_needed]}
    for s in slots:
        s["selected"] = s["start"] in cheap_starts
    return slots


def kwh_needed(current_soc: float, target_soc: float, capacity: float, efficiency: float) -> float:
    return max(0.0, (target_soc - current_soc) / 100 * capacity / efficiency)


def slots_needed_count(kwh: float, max_amp: int, slot_hours: float = 0.25) -> int:
    max_kw = max_amp * 0.23
    return math.ceil(kwh / (max_kw * slot_hours))


class TestKwhCalculation:
    def test_basic(self):
        assert round(kwh_needed(50, 80, 64, 0.9), 2) == round(30 / 100 * 64 / 0.9, 2)

    def test_already_at_target(self):
        assert kwh_needed(80, 80, 64, 0.9) == 0.0

    def test_above_target(self):
        assert kwh_needed(90, 80, 64, 0.9) == 0.0

    def test_efficiency_increases_kwh(self):
        assert kwh_needed(50, 80, 64, 0.8) > kwh_needed(50, 80, 64, 0.9)


class TestSlotSelection:
    def _make_future_slots(self, n: int, base_price: float = 1.0) -> list[dict]:
        now = datetime.now(_UTC)
        slots = []
        for i in range(n):
            start = (now + timedelta(hours=i)).isoformat()
            slots.append(_make_slot(start, base_price + i * 0.1))
        return slots

    def test_selects_cheapest(self):
        slots = self._make_future_slots(4)
        result = select_cheapest_slots(slots, 2)
        selected = [s for s in result if s["selected"]]
        assert len(selected) == 2
        # The two cheapest should be the first two (lowest prices)
        prices = sorted(s["price"] for s in selected)
        all_prices = sorted(s["price"] for s in slots)
        assert prices == all_prices[:2]

    def test_select_all_when_fewer_than_needed(self):
        slots = self._make_future_slots(2)
        result = select_cheapest_slots(slots, 5)
        assert all(s["selected"] for s in result)

    def test_no_slots(self):
        result = select_cheapest_slots([], 3)
        assert result == []


class TestAmpAdjust:
    def _available_amp(
        self,
        phases: list[float],
        charger_phase: int,
        last_sent_amp: int,
        breaker_limit: int,
        min_amp: int,
        max_amp: int,
    ) -> int | None:
        """Reproduce coordinator amp-adjust calculation."""
        ph = list(phases)
        ph[charger_phase - 1] = max(0.0, ph[charger_phase - 1] - last_sent_amp)
        headroom = min(breaker_limit - p for p in ph)
        new_amp = int(max(min_amp, min(max_amp, round(headroom))))
        return new_amp

    def test_normal_headroom(self):
        # L1=10, L2=5, L3=5, charger on L1 drawing 8A, breaker=20
        amp = self._available_amp([10, 5, 5], 1, 8, 20, 6, 16)
        # L1 baseline = 10-8=2, headroom = min(20-2, 20-5, 20-5) = min(18,15,15) = 15 → clamped to 16
        assert amp == 15

    def test_clamps_to_max(self):
        amp = self._available_amp([1, 1, 1], 1, 0, 20, 6, 16)
        assert amp == 16

    def test_clamps_to_min(self):
        amp = self._available_amp([18, 18, 18], 1, 0, 20, 6, 16)
        assert amp == 6

    def test_charger_phase_contribution_subtracted(self):
        # Charger drawing 10A on L2; without subtraction L2 has no headroom
        amp_without = self._available_amp([5, 18, 5], 2, 0, 20, 6, 16)
        amp_with = self._available_amp([5, 18, 5], 2, 10, 20, 6, 16)
        assert amp_with > amp_without


# -----------------------------------------------------------------------
# Cheap price threshold logic (extracted for unit testing)
# -----------------------------------------------------------------------

def _is_current_slot_cheap(cheap_threshold: float, current_price_data: list[dict]) -> bool:
    """Mirror of coordinator._is_current_slot_cheap, without HA dependencies."""
    if cheap_threshold <= 0:
        return False
    now = datetime.now(_UTC)
    sorted_prices = sorted(current_price_data, key=lambda e: datetime.fromisoformat(e["start"]))
    current_idx = None
    for i, entry in enumerate(sorted_prices):
        start = datetime.fromisoformat(entry["start"])
        end = datetime.fromisoformat(entry["end"])
        if start <= now < end:
            current_idx = i
            break
    if current_idx is None:
        return False
    consecutive_slots = 0
    for entry in sorted_prices[current_idx:]:
        if entry["value"] <= cheap_threshold:
            consecutive_slots += 1
        else:
            break
    return consecutive_slots >= 4


def _make_price_entry(offset_minutes: int, duration_minutes: int, price: float) -> dict:
    """Build a raw Nordpool-style price entry relative to now."""
    now = datetime.now(_UTC)
    start = now + timedelta(minutes=offset_minutes)
    end = start + timedelta(minutes=duration_minutes)
    return {"start": start.isoformat(), "end": end.isoformat(), "value": price}


class TestCheapThreshold:
    def test_disabled_at_zero(self):
        """Threshold = 0 → always returns False regardless of prices."""
        prices = [_make_price_entry(-5, 15, 0.10) for _ in range(6)]
        assert _is_current_slot_cheap(0.0, prices) is False

    def test_requires_1h_block(self):
        """3 consecutive cheap slots (45 min) is not enough — need 4 (1 h)."""
        # Current slot starts 5 min ago; 3 slots cheap, then one expensive
        prices = [
            _make_price_entry(-5, 15, 0.50),   # current slot, cheap
            _make_price_entry(10, 15, 0.50),    # +1, cheap
            _make_price_entry(25, 15, 0.50),    # +2, cheap (3 total)
            _make_price_entry(40, 15, 2.00),    # +3, expensive
            _make_price_entry(55, 15, 0.50),    # +4, cheap again (but block broken)
        ]
        assert _is_current_slot_cheap(1.0, prices) is False

    def test_activates_at_4_slots(self):
        """Exactly 4 consecutive cheap slots (1 hour) → returns True."""
        prices = [
            _make_price_entry(-5, 15, 0.50),   # current
            _make_price_entry(10, 15, 0.50),
            _make_price_entry(25, 15, 0.50),
            _make_price_entry(40, 15, 0.50),   # 4th slot
            _make_price_entry(55, 15, 2.00),   # expensive (doesn't matter)
        ]
        assert _is_current_slot_cheap(1.0, prices) is True

    def test_block_broken(self):
        """cheap, cheap, expensive, cheap → False because block broken at slot 3."""
        prices = [
            _make_price_entry(-5, 15, 0.50),   # current, cheap
            _make_price_entry(10, 15, 0.50),    # cheap
            _make_price_entry(25, 15, 2.00),    # expensive — breaks block
            _make_price_entry(40, 15, 0.50),    # cheap (separate block)
            _make_price_entry(55, 15, 0.50),    # cheap
        ]
        assert _is_current_slot_cheap(1.0, prices) is False
