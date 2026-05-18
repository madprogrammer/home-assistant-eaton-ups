"""Support for Eaton UPS switches."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import (
    RECEP_TOGGLE_DELAY_SECONDS,
    SNMP_OID_CONTROL_OUTPUT_OFF_DELAY,
    SNMP_OID_CONTROL_OUTPUT_ON_DELAY,
    SNMP_OID_OUTPUT_SOURCE,
    SNMP_OID_RECEP_COUNT,
    SNMP_OID_RECEP_OFF_DELAY,
    SNMP_OID_RECEP_ON_DELAY,
    SNMP_OID_RECEP_STATUS,
    UPS_SHUTDOWN_CANCEL_VALUE,
    UPS_SHUTDOWN_DELAY_SECONDS,
    UPS_STARTUP_DELAY_SECONDS,
    OutputSource,
    ReceptacleStatus,
)
from .coordinator import SnmpCoordinator
from .entity import SnmpEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1

_OUTPUT_OFF_SOURCES = {OutputSource.other.value, OutputSource.none.value}

_STARTUP_PENDING_WINDOW = 10
_SHUTDOWN_PENDING_WINDOW = UPS_SHUTDOWN_DELAY_SECONDS + 5
_CANCEL_PENDING_WINDOW = 5
_RECEP_PENDING_WINDOW = 6


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the switches."""

    coordinator = entry.runtime_data
    entities: list[SwitchEntity] = [SnmpUpsOutputSwitchEntity(coordinator)]

    for index in range(1, coordinator.data.get(SNMP_OID_RECEP_COUNT, 0) + 1):
        entities.append(SnmpReceptacleSwitchEntity(coordinator, index))

    async_add_entities(entities)


class _TransitionMixin:
    """Adds an optimistic 'pending' state with a timed unavailability window.

    The polling coordinator can lag the actual UPS state by tens of seconds,
    and the firmware countdown OIDs can decrement faster than we can read
    them — so a click would otherwise produce no visible feedback. Instead
    we set a local flag immediately, show the expected on/off state, mark
    the entity unavailable, and clear the flag after a generous window
    (then trigger a refresh to settle on real polled state).
    """

    _pending: bool = False
    _pending_is_on: bool = False
    _pending_cancel: Any = None

    def _begin_pending(self, expected_is_on: bool, window_seconds: float) -> None:
        if self._pending_cancel is not None:
            self._pending_cancel()
            self._pending_cancel = None

        self._pending = True
        self._pending_is_on = expected_is_on
        self.async_write_ha_state()

        @callback
        def _clear(_now):
            self._pending_cancel = None
            self._pending = False
            self.async_write_ha_state()
            self.hass.async_create_task(self.coordinator.async_refresh())

        self._pending_cancel = async_call_later(self.hass, window_seconds, _clear)


class SnmpUpsOutputSwitchEntity(_TransitionMixin, SnmpEntity, SwitchEntity):
    """Switch representing the UPS output as a whole.

    State is derived from xupsOutputSource (534.1.4.5) — the 9SX does not
    implement xupsOutputStatus (534.1.4.10), so the source field is the
    only reliable signal: 2 = none (output off), 3..12 = some real power
    source (output on).

    Turn-on picks between OffDelay=-1 (cancel a pending shutdown — the
    only case where -1 is accepted by the firmware) and OnDelay=1 (cold
    start when the output is actually off; OnDelay=0 is a no-op on this
    firmware).
    """

    _attr_device_class = SwitchDeviceClass.OUTLET
    _attr_icon = "mdi:power-plug"

    _name_suffix = "Output"
    _value_oid = SNMP_OID_CONTROL_OUTPUT_OFF_DELAY

    def _int(self, oid: str, default: int = 0) -> int:
        try:
            return int(self.coordinator.data.get(oid))
        except (TypeError, ValueError):
            return default

    def _output_powered(self) -> bool:
        source = self._int(SNMP_OID_OUTPUT_SOURCE, default=OutputSource.none.value)
        return source not in _OUTPUT_OFF_SOURCES

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return not self._pending

    @property
    def is_on(self) -> bool:
        if self._pending:
            return self._pending_is_on
        if self._int(SNMP_OID_CONTROL_OUTPUT_OFF_DELAY) > 0:
            return False
        return self._output_powered()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Cancel a pending shutdown or power the output back up."""
        if self._int(SNMP_OID_CONTROL_OUTPUT_OFF_DELAY) > 0:
            self._begin_pending(True, _CANCEL_PENDING_WINDOW)
            await self.coordinator._api.set(
                [(SNMP_OID_CONTROL_OUTPUT_OFF_DELAY, UPS_SHUTDOWN_CANCEL_VALUE)]
            )
        elif not self._output_powered():
            self._begin_pending(True, _STARTUP_PENDING_WINDOW)
            await self.coordinator._api.set(
                [(SNMP_OID_CONTROL_OUTPUT_ON_DELAY, UPS_STARTUP_DELAY_SECONDS)]
            )
        # else: already on, no shutdown pending → no-op
        await self.coordinator.async_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Initiate a delayed UPS shutdown."""
        self._begin_pending(False, _SHUTDOWN_PENDING_WINDOW)
        await self.coordinator._api.set(
            [(SNMP_OID_CONTROL_OUTPUT_OFF_DELAY, UPS_SHUTDOWN_DELAY_SECONDS)]
        )
        await self.coordinator.async_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()


class SnmpReceptacleSwitchEntity(_TransitionMixin, SnmpEntity, SwitchEntity):
    """Switch that toggles a single UPS output group (receptacle)."""

    _attr_device_class = SwitchDeviceClass.OUTLET

    _name_prefix = "Output Group"
    _value_oid = SNMP_OID_RECEP_STATUS

    def __init__(self, coordinator: SnmpCoordinator, index: int) -> None:
        self._name_suffix = str(index)
        super().__init__(coordinator, index)
        self._on_delay_oid = SNMP_OID_RECEP_ON_DELAY.replace("index", str(index))
        self._off_delay_oid = SNMP_OID_RECEP_OFF_DELAY.replace("index", str(index))

    def _status(self) -> int | None:
        try:
            return int(self.coordinator.data.get(self._value_oid))
        except (TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return not self._pending

    @property
    def is_on(self) -> bool:
        if self._pending:
            return self._pending_is_on
        status = self._status()
        if status is None:
            return False
        return status in (ReceptacleStatus.on.value, ReceptacleStatus.pending_off.value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._begin_pending(True, _RECEP_PENDING_WINDOW)
        await self.coordinator._api.set(
            [(self._on_delay_oid, RECEP_TOGGLE_DELAY_SECONDS)]
        )
        await self.coordinator.async_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._begin_pending(False, _RECEP_PENDING_WINDOW)
        await self.coordinator._api.set(
            [(self._off_delay_oid, RECEP_TOGGLE_DELAY_SECONDS)]
        )
        await self.coordinator.async_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()
