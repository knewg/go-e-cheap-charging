"""Root conftest.py — installs HA stubs and shared test infrastructure.

This file MUST be at the project root (not inside tests/) so that pytest
loads it before collecting any test module. The critical reason:

  test_coordinator.py contains module-level code that runs at collection
  time and sets DataUpdateCoordinator = object, which breaks coordinator
  instantiation. By forcing coordinator.py to be imported HERE — after we
  install a proper _DUCStub that sets self.hass — the ChargingCoordinator
  class is defined with the correct base class. Python class hierarchies
  are fixed at definition time; the later reassignment in test_coordinator.py
  has no effect on the already-defined class.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# 1. Stub every HA module that coordinator.py (and its drivers) import
# ---------------------------------------------------------------------------

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
    "homeassistant.const",
]:
    if mod not in sys.modules:
        sys.modules[mod] = types.ModuleType(mod)

# ---------------------------------------------------------------------------
# 2. DataUpdateCoordinator stub — must set self.hass for ChargingCoordinator
# ---------------------------------------------------------------------------

class _DUCStub:
    """Minimal stub for DataUpdateCoordinator that forwards hass to self."""

    def __init__(self, hass=None, *args, **kwargs):
        self.hass = hass


sys.modules["homeassistant.helpers.update_coordinator"].DataUpdateCoordinator = _DUCStub

# ---------------------------------------------------------------------------
# 3. Core HA stubs
# ---------------------------------------------------------------------------

_CoreState = type("CoreState", (), {"running": "running", "starting": "starting"})
sys.modules["homeassistant.core"].CoreState = _CoreState
sys.modules["homeassistant.core"].callback = lambda f: f
sys.modules["homeassistant.core"].HomeAssistant = object
sys.modules["homeassistant.const"].EVENT_HOMEASSISTANT_STARTED = "homeassistant_started"
sys.modules["homeassistant.config_entries"].ConfigEntry = object

# ---------------------------------------------------------------------------
# 4. Event / timer stubs — return callable cancel handles
# ---------------------------------------------------------------------------

def _fake_async_call_later(hass, delay, callback):
    return lambda: None


def _fake_async_track_state_change_event(*args, **kwargs):
    return lambda: None


sys.modules["homeassistant.helpers.event"].async_call_later = _fake_async_call_later
sys.modules["homeassistant.helpers.event"].async_track_state_change_event = (
    _fake_async_track_state_change_event
)

# ---------------------------------------------------------------------------
# 5. dt_util stubs — now() is patchable per test via freeze_now()
# ---------------------------------------------------------------------------

_UTC = timezone.utc
_dt_util = sys.modules["homeassistant.util.dt"]
_dt_util.now = lambda: datetime.now(_UTC)
_dt_util.as_local = lambda dt: dt
_dt_util.parse_datetime = lambda s: datetime.fromisoformat(s)
_dt_util.DEFAULT_TIME_ZONE = _UTC

# ---------------------------------------------------------------------------
# 6. MQTT stubs — publish is captured into a per-test list via fixture
# ---------------------------------------------------------------------------

_MQTT_LOG: list[dict] = []


async def _fake_mqtt_publish(hass, topic, payload):
    _MQTT_LOG.append({"topic": topic, "payload": json.loads(payload)})


async def _fake_mqtt_subscribe(*args, **kwargs):
    return lambda: None


_mqtt_mod = sys.modules["homeassistant.components.mqtt"]
_mqtt_mod.async_publish = _fake_mqtt_publish
_mqtt_mod.async_subscribe = _fake_mqtt_subscribe

# ---------------------------------------------------------------------------
# 7. Entity registry stubs (KiaUvoDriver uses these)
# ---------------------------------------------------------------------------

_er_mod = sys.modules["homeassistant.helpers.entity_registry"]
_er_mod.async_get = lambda *a, **k: MagicMock()
_er_mod.async_entries_for_device = lambda *a, **k: []
sys.modules["homeassistant.helpers"].entity_registry = _er_mod

# ---------------------------------------------------------------------------
# 8. Force-import coordinator.py NOW so ChargingCoordinator is defined with
#    _DUCStub as its base — BEFORE test_coordinator.py can mutate the stub.
# ---------------------------------------------------------------------------

import custom_components.goe_cheap_charging.coordinator  # noqa: E402, F401

# ---------------------------------------------------------------------------
# Fake infrastructure — shared between all simulation tests
# ---------------------------------------------------------------------------


class FakeState:
    def __init__(self, state, attributes=None):
        self.state = str(state)
        self.attributes = attributes or {}


class FakeStateRegistry:
    def __init__(self):
        self._states: dict = {}

    def get(self, entity_id: str):
        return self._states.get(entity_id)

    def set(self, entity_id: str, state, **attrs):
        self._states[entity_id] = FakeState(str(state), attrs)


class FakeServices:
    """Records all service calls; returns nordpool price data on demand."""

    def __init__(self):
        self.calls: list[dict] = []
        self._nordpool: dict[str, list] = {}  # date_str → milli-SEK price list

    def set_nordpool(self, date_str: str, prices_milli_sek: list):
        self._nordpool[date_str] = prices_milli_sek

    async def async_call(
        self,
        domain: str,
        service: str,
        data=None,
        blocking=True,
        return_response=False,
    ):
        self.calls.append({"domain": domain, "service": service, "data": data or {}})
        if domain == "nordpool" and return_response:
            return self._nordpool.get((data or {}).get("date", ""), [])
        return None


class FakeBus:
    def async_listen_once(self, event, callback):
        pass


class FakeConfigEntry:
    def __init__(self):
        self.entry_id = "nordpool_fake"


class FakeConfigEntries:
    def async_entries(self, domain: str):
        return [FakeConfigEntry()] if domain == "nordpool" else []


class FakeHass:
    """Minimal Home Assistant environment for coordinator tests."""

    def __init__(self):
        self.states = FakeStateRegistry()
        self.services = FakeServices()
        self.config_entries = FakeConfigEntries()
        self.bus = FakeBus()
        self.state = _CoreState.running
        self._tasks: list = []

    def async_create_task(self, coro):
        task = asyncio.ensure_future(coro)
        self._tasks.append(task)
        return task

    async def drain_tasks(self):
        """Run pending tasks to completion, including tasks spawned by tasks."""
        while self._tasks:
            pending = list(self._tasks)
            self._tasks.clear()
            await asyncio.gather(*pending, return_exceptions=True)


class FakeCar:
    """Replaces KiaUvoDriver in tests — returns controllable SoC."""

    def __init__(self, soc: float = 50.0):
        self._soc = soc
        self.force_update_calls: int = 0
        self.set_charge_limit_calls: list[int] = []

    @property
    def soc_entity_id(self):
        return "sensor.fake_car_ev_battery_level"

    def get_soc(self) -> float:
        return self._soc

    def get_charge_limit(self):
        return None

    async def async_force_update(self):
        self.force_update_calls += 1

    async def async_set_charge_limit(self, limit_pct: int):
        self.set_charge_limit_calls.append(limit_pct)


class FakeMqttMsg:
    """Simulates a received MQTT message (passed to _handle_mqtt_message)."""

    def __init__(self, topic: str, payload: str):
        self.topic = topic
        self.payload = payload


# ---------------------------------------------------------------------------
# Config / Coordinator builders
# ---------------------------------------------------------------------------

from custom_components.goe_cheap_charging.const import (  # noqa: E402
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
)
from custom_components.goe_cheap_charging.coordinator import ChargingCoordinator  # noqa: E402


def make_config(**overrides) -> dict:
    """Return a config-entry data dict with sensible defaults."""
    defaults = {
        CONF_CHARGER_SERIAL: "XYZ123",
        CONF_BATTERY_CAPACITY: 64.0,
        CONF_EFFICIENCY: 0.9,   # stored as fraction, not percent
        CONF_BREAKER_LIMIT: 20,
        CONF_CHARGER_PHASE: 1,
        CONF_CHARGER_N_PHASES: 3,
        CONF_MIN_AMP: 6,
        CONF_MAX_AMP: 16,
        CONF_PHASE_L1_ENTITY: "sensor.l1",
        CONF_PHASE_L2_ENTITY: "sensor.l2",
        CONF_PHASE_L3_ENTITY: "sensor.l3",
    }
    defaults.update(overrides)
    return defaults


class FakeEntry:
    def __init__(self, cfg: dict | None = None):
        self.data = cfg if cfg is not None else make_config()


def make_coordinator(
    hass: FakeHass,
    cfg: dict | None = None,
    soc: float = 50.0,
    smart_enabled: bool = True,
) -> ChargingCoordinator:
    """Create a ChargingCoordinator wired with a FakeCar and FakeHass."""
    entry = FakeEntry(cfg)
    coord = ChargingCoordinator(hass, entry)
    coord.car = FakeCar(soc=soc)
    coord._active_car_is_guest = False
    coord._smart_enabled = smart_enabled
    return coord


# ---------------------------------------------------------------------------
# Price data factory
# ---------------------------------------------------------------------------

def make_nordpool_prices(prices_sek: list[float], base_dt: datetime) -> list[dict]:
    """Build a Nordpool-style price list.

    prices_sek: per-15-min prices in SEK/kWh.
    Returns entries with price in milli-SEK (coordinator divides by 1000).
    """
    entries = []
    dt = base_dt
    for price in prices_sek:
        end = dt + timedelta(minutes=15)
        entries.append(
            {"start": dt.isoformat(), "end": end.isoformat(), "price": price * 1000}
        )
        dt = end
    return entries


def set_day_config(
    hass: FakeHass,
    day: str,
    departure: str | None,
    target_soc: int = 80,
    manual_kwh: float = 0.0,
) -> None:
    """Set HA entity states for a given weekday's charging config.

    departure: "HH:MM" string or None (leaves entity as unavailable).
    """
    from custom_components.goe_cheap_charging.const import DOMAIN

    prefix = f"{DOMAIN}_"
    if departure is not None:
        hass.states.set(
            f"time.{prefix}{day}_departure", f"{departure}:00"
        )
    hass.states.set(f"number.{prefix}{day}_target_soc", str(target_soc))
    hass.states.set(f"number.{prefix}{day}_manual_kwh", str(manual_kwh))


def set_smart_enabled(hass: FakeHass, enabled: bool) -> None:
    from custom_components.goe_cheap_charging.const import DOMAIN
    hass.states.set(f"switch.{DOMAIN}_smart_enabled", "on" if enabled else "off")


# ---------------------------------------------------------------------------
# Time-freeze helper
# ---------------------------------------------------------------------------

@contextmanager
def freeze_now(dt: datetime):
    """Freeze dt_util.now() to *dt* for the duration of the context."""
    _dt_util.now = lambda: dt
    try:
        yield dt
    finally:
        _dt_util.now = lambda: datetime.now(_UTC)


# ---------------------------------------------------------------------------
# MQTT helper
# ---------------------------------------------------------------------------

def mqtt_commands(log: list[dict]) -> dict[str, object]:
    """Extract {key: value} from the MQTT publish log.

    Topic format: go-eCharger/SERIAL/KEY/set
    """
    result: dict = {}
    for entry in log:
        parts = entry["topic"].split("/")
        if len(parts) == 4 and parts[3] == "set":
            result[parts[2]] = entry["payload"]
    return result


def all_mqtt_keys(log: list[dict]) -> list[str]:
    """Return ordered list of MQTT keys published (may contain duplicates)."""
    keys = []
    for entry in log:
        parts = entry["topic"].split("/")
        if len(parts) == 4 and parts[3] == "set":
            keys.append(parts[2])
    return keys


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_mqtt_log():
    """Clear the global MQTT publish log before every test."""
    _MQTT_LOG.clear()
    yield


@pytest.fixture
def mqtt_log():
    """Return reference to the per-test MQTT publish log."""
    return _MQTT_LOG


@pytest.fixture
def fake_hass():
    return FakeHass()
