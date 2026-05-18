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
    SNMP_OID_RECEP_COUNT,
    SNMP_OID_RECEP_OFF_DELAY,
    SNMP_OID_RECEP_ON_DELAY,
    SNMP_OID_RECEP_STATUS,
    UPS_SHUTDOWN_CANCEL_VALUE,
    UPS_SHUTDOWN_DELAY_SECONDS,
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


class SnmpUpsOutputSwitchEntity(SnmpEntity, SwitchEntity):
    """Switch representing the UPS output as a whole.

    ON  → output is running, with no shutdown pending. Flipping the switch on
          while a shutdown countdown is active writes -1 to
          xupsControlOutputOffDelay, which cancels the shutdown.
    OFF → flipping the switch off writes UPS_SHUTDOWN_DELAY_SECONDS to
          xupsControlOutputOffDelay, starting a countdown to power down the
          output. While the countdown is running the switch reads as OFF.

    On this firmware the OID reads 0 when idle (no shutdown pending) and a
    positive value while a shutdown countdown is active.
    """

    _attr_device_class = SwitchDeviceClass.OUTLET
    _attr_icon = "mdi:power-plug"

    _name_suffix = "Output"
    _value_oid = SNMP_OID_CONTROL_OUTPUT_OFF_DELAY

    @property
    def is_on(self) -> bool:
        """Return True while no shutdown countdown is in progress."""
        value = self.coordinator.data.get(self._value_oid)
        try:
            return int(value) <= 0
        except (TypeError, ValueError):
            return True

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Cancel any pending UPS shutdown."""
        await self.coordinator._api.set(
            [(SNMP_OID_CONTROL_OUTPUT_OFF_DELAY, UPS_SHUTDOWN_CANCEL_VALUE)]
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Initiate a delayed UPS shutdown."""
        await self.coordinator._api.set(
            [(SNMP_OID_CONTROL_OUTPUT_OFF_DELAY, UPS_SHUTDOWN_DELAY_SECONDS)]
        )
        await self.coordinator.async_request_refresh()

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
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Power the receptacle off (immediate)."""
        await self.coordinator._api.set(
            [(self._off_delay_oid, RECEP_TOGGLE_DELAY_SECONDS)]
        )
        await self.coordinator.async_request_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()
