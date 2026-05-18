"""Support for Eaton UPS switches."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

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


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the switches."""

    coordinator = entry.runtime_data
    entities: list[SwitchEntity] = [SnmpUpsOutputSwitchEntity(coordinator)]

    for index in range(1, coordinator.data.get(SNMP_OID_RECEP_COUNT, 0) + 1):
        entities.append(SnmpReceptacleSwitchEntity(coordinator, index))

    async_add_entities(entities)


_OUTPUT_OFF_SOURCES = {OutputSource.other.value, OutputSource.none.value}


class SnmpUpsOutputSwitchEntity(SnmpEntity, SwitchEntity):
    """Switch representing the UPS output as a whole.

    ON  → output is currently powered (xupsOutputSource reports a real
          source like normal/bypass/battery/...) and no shutdown countdown
          is active.
    OFF → either xupsControlOutputOffDelay has a positive countdown
          running, or xupsOutputSource reports `none`/`other`.

    On the 9SX firmware xupsOutputStatus (534.1.4.10) is not implemented,
    so we key off xupsOutputSource (534.1.4.5) instead — that returns 2
    when the output is down and 3..12 when it is powered by some source.

    Turn-on chooses between OffDelay=-1 (cancel a pending shutdown — the
    only case where -1 is accepted; the firmware otherwise responds
    badValue) and OnDelay=0 (cold start when output is actually off).
    Turn-off always writes UPS_SHUTDOWN_DELAY_SECONDS to OffDelay.
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
    def is_on(self) -> bool:
        """Return True only when the output is up and no shutdown is pending."""
        if self._int(SNMP_OID_CONTROL_OUTPUT_OFF_DELAY) > 0:
            return False
        return self._output_powered()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Cancel a pending shutdown or power the output back up."""
        if self._int(SNMP_OID_CONTROL_OUTPUT_OFF_DELAY) > 0:
            await self.coordinator._api.set(
                [(SNMP_OID_CONTROL_OUTPUT_OFF_DELAY, UPS_SHUTDOWN_CANCEL_VALUE)]
            )
        elif not self._output_powered():
            await self.coordinator._api.set(
                [(SNMP_OID_CONTROL_OUTPUT_ON_DELAY, UPS_STARTUP_DELAY_SECONDS)]
            )
        await self.coordinator.async_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Initiate a delayed UPS shutdown."""
        await self.coordinator._api.set(
            [(SNMP_OID_CONTROL_OUTPUT_OFF_DELAY, UPS_SHUTDOWN_DELAY_SECONDS)]
        )
        await self.coordinator.async_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()


class SnmpReceptacleSwitchEntity(SnmpEntity, SwitchEntity):
    """Switch that toggles a single UPS output group (receptacle)."""

    _attr_device_class = SwitchDeviceClass.OUTLET

    _name_prefix = "Output Group"
    _value_oid = SNMP_OID_RECEP_STATUS

    def __init__(self, coordinator: SnmpCoordinator, index: int) -> None:
        """Initialize a receptacle switch."""
        self._name_suffix = str(index)
        super().__init__(coordinator, index)
        self._on_delay_oid = SNMP_OID_RECEP_ON_DELAY.replace("index", str(index))
        self._off_delay_oid = SNMP_OID_RECEP_OFF_DELAY.replace("index", str(index))

    @property
    def is_on(self) -> bool:
        """Return True when the receptacle is powered (or about to be)."""
        value = self.coordinator.data.get(self._value_oid)
        try:
            status = int(value)
        except (TypeError, ValueError):
            return False
        return status in (ReceptacleStatus.on.value, ReceptacleStatus.pending_off.value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Power the receptacle on (immediate)."""
        await self.coordinator._api.set(
            [(self._on_delay_oid, RECEP_TOGGLE_DELAY_SECONDS)]
        )
        await self.coordinator.async_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Power the receptacle off (immediate)."""
        await self.coordinator._api.set(
            [(self._off_delay_oid, RECEP_TOGGLE_DELAY_SECONDS)]
        )
        await self.coordinator.async_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()
