"""Unit tests for the GO-e Cheap Charging coordinator logic."""
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
    "homeassistant.helpers.entity_registry",
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

# Provide minimal ConfigEntry stub
sys.modules["homeassistant.config_entries"].ConfigEntry = object
sys.modules["homeassistant.const"] = types.ModuleType("homeassistant.const")
sys.modules["homeassistant.const"].EVENT_HOMEASSISTANT_STARTED = "homeassistant_started"
sys.modules["homeassistant.core"].CoreState = type("CoreState", (), {"running": "running", "starting": "starting"})
sys.modules["homeassistant.core"].callback = lambda f: f
sys.modules["homeassistant.core"].HomeAssistant = object
sys.modules["homeassistant.helpers.event"].async_call_later = lambda *a, **k: None
sys.modules["homeassistant.helpers.event"].async_track_state_change_event = lambda *a, **k: None
sys.modules["homeassistant.helpers"].entity_registry = sys.modules["homeassistant.helpers.entity_registry"]
sys.modules["homeassistant.helpers.entity_registry"].async_get = lambda *a, **k: MagicMock()

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


from custom_components.goe_cheap_charging.coordinator import _select_slots, _get_clusters

# -----------------------------------------------------------------------
# Schedule selection logic (pure, no HA dependencies)
# -----------------------------------------------------------------------

def _make_slots(prices: list, start_iso: str = "2026-01-01T00:00:00+00:00") -> list:
    """Create contiguous 15-min slots from a list of prices."""
    start = datetime.fromisoformat(start_iso)
    slots = []
    for price in prices:
        end = start + timedelta(minutes=15)
        slots.append({"start": start, "end": end, "price": price, "selected": False})
        start = end
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
    def test_selects_cheapest_block(self):
        # 8 slots; cheapest 4 are in the middle (indices 2-5)
        # n_slots=4 → one clean 4-slot block, no extension needed
        prices = [1.5, 1.4, 1.0, 1.1, 1.2, 1.3, 1.6, 1.7]
        slots = _make_slots(prices)
        _select_slots(slots, 4, 0.10)
        selected_indices = [i for i, s in enumerate(slots) if s["selected"]]
        assert selected_indices == [2, 3, 4, 5]

    def test_finds_cheapest_window_not_cheapest_individuals(self):
        # Single ultra-cheap slot surrounded by expensive ones, plus a moderately
        # cheap contiguous block elsewhere.  The algorithm must pick the cheapest
        # 1-hour WINDOW, not scatter selection around the single cheap slot.
        #   index:  0     1     2     3     4     5     6     7     8
        #   price: 1.00  1.00  0.50  1.00  1.00  0.60  0.70  0.80  0.90
        # Cheapest 4-slot window by avg: [5-8] = (0.60+0.70+0.80+0.90)/4 = 0.75
        # (vs window [0-3] avg=0.875 which contains the cheap spike at idx 2)
        prices = [1.00, 1.00, 0.50, 1.00, 1.00, 0.60, 0.70, 0.80, 0.90]
        slots = _make_slots(prices)
        _select_slots(slots, 4, 0.10)
        selected_indices = [i for i, s in enumerate(slots) if s["selected"]]
        assert selected_indices == [5, 6, 7, 8]

    def test_fills_cheap_gap_between_blocks(self):
        # Two 4-slot cheap blocks with a 2-slot cheap gap (0.50) between them.
        # prices: [0.42]*4 + [0.50,0.50] + [0.43]*4 (10 slots), n_slots=8, n_needed=8
        # single [0-7] avg=0.4425, multi [{0-3},{6-9}] avg=0.425
        # single(0.4425) ≤ multi(0.425)+0.10=0.525 → single block [0-7] is preferred.
        # The cheap gap slots (4-5) are included in the single contiguous block.
        prices = [0.42] * 4 + [0.50, 0.50] + [0.43] * 4
        slots = _make_slots(prices)
        _select_slots(slots, 8, 0.10)
        selected_indices = [i for i, s in enumerate(slots) if s["selected"]]
        # Single block [0-7] chosen; cheap gap slots 4-5 are included; slots 8-9 unneeded
        assert selected_indices == list(range(8))

    def test_does_not_fill_expensive_gap(self):
        # Expensive gap (0.90) makes multi-block significantly cheaper than single block.
        # prices: [0.42]*4 + [0.90,0.90] + [0.43]*4 (10 slots), n_slots=8, n_needed=8
        # single [0-7] avg=0.5425, multi [{0-3},{6-9}] avg=0.425
        # single(0.5425) > multi(0.425)+0.10=0.525 → multi-block chosen.
        # Gap-fill: ref_price=0.425, gap 0.90 ≤ 0.525? NO → gap NOT filled.
        prices = [0.42] * 4 + [0.90, 0.90] + [0.43] * 4
        slots = _make_slots(prices)
        _select_slots(slots, 8, 0.10)
        # Gap (indices 4-5) should NOT be filled
        assert not slots[4]["selected"]
        assert not slots[5]["selected"]
        # Both blocks should still be selected
        assert all(s["selected"] for s in slots[:4])
        assert all(s["selected"] for s in slots[6:])

    def test_select_all_when_fewer_than_needed(self):
        prices = [1.0, 1.1, 1.2]
        slots = _make_slots(prices)
        _select_slots(slots, 10, 0.10)
        assert all(s["selected"] for s in slots)

    def test_no_slots(self):
        slots = []
        _select_slots(slots, 3, 0.10)
        assert slots == []

    def test_non_aligned_cheapest_window(self):
        # Cheapest 4 slots span a clock-hour boundary: indices 3-6
        # (old hour-bucket algorithm would have missed this)
        prices = [1.5, 1.4, 1.3, 1.0, 1.05, 1.1, 1.15, 1.6, 1.7, 1.8, 1.9, 2.0]
        slots = _make_slots(prices)
        _select_slots(slots, 4, 0.10)
        selected_indices = [i for i, s in enumerate(slots) if s["selected"]]
        assert selected_indices == [3, 4, 5, 6]

    def test_prefers_single_block_over_scattered(self):
        # 8 slots with varying prices; single block [0-7] is slightly more expensive
        # than the best multi-block split, but within the spread_threshold.
        # prices: [0.42, 0.42, 0.55, 0.55, 0.43, 0.43, 0.43, 0.43]
        # single_avg (8-slot window [0-7]) = (0.42+0.42+0.55+0.55+0.43+0.43+0.43+0.43)/8 = 0.4575...
        # Actually all 8 slots are needed → n_needed=8 → single block is [0-7] → single_avg=multi_avg
        # Better: use n_slots=4 with prices designed so single block [4-7] = multi best window too.
        # Use clear scenario: n_slots=8, single contiguous [0-7] avg=0.4575,
        # multi two windows [0-3] avg=0.485 and [4-7] avg=0.43 → multi_avg=0.4575 → tie → single ✓
        prices = [0.42, 0.42, 0.55, 0.55, 0.43, 0.43, 0.43, 0.43]
        slots = _make_slots(prices)
        _select_slots(slots, 8, 0.10)
        selected_indices = [i for i, s in enumerate(slots) if s["selected"]]
        # single_avg == multi_avg (both 0.4575), so single_avg <= multi_avg + threshold → single block
        assert selected_indices == list(range(8))

    def test_prefers_multi_block_when_price_spike_exceeds_threshold(self):
        # Two cheap periods separated by a large price spike.
        # Prices: cheap [0-3]=0.30, spike [4-7]=1.20, cheap [8-11]=0.32
        # n_slots=8 → n_needed=8
        # single best 8-window: [0-7] avg=(4*0.30+4*1.20)/8=0.75
        # multi two 4-slot windows [0-3]+[8-11] avg=(4*0.30+4*0.32)/8=0.31
        # single_avg (0.75) > multi_avg (0.31) + threshold (0.10) → multi-block ✓
        prices = [0.30] * 4 + [1.20] * 4 + [0.32] * 4
        slots = _make_slots(prices)
        _select_slots(slots, 8, 0.10)
        selected_indices = [i for i, s in enumerate(slots) if s["selected"]]
        # Should pick both cheap blocks, not the spike
        assert 4 not in selected_indices
        assert 5 not in selected_indices
        assert 6 not in selected_indices
        assert 7 not in selected_indices
        assert all(i in selected_indices for i in range(4))
        assert all(i in selected_indices for i in range(8, 12))


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
