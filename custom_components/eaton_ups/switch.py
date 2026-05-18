"""Support for Eaton UPS switches."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_time_interval

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

_POLL_INTERVAL = timedelta(seconds=2)
# Safety-net timeouts — if the UPS never confirms the expected state we
# still want the UI to recover instead of staying stuck-disabled forever.
_STARTUP_FALLBACK_SECONDS = 30
_SHUTDOWN_FALLBACK_SECONDS = UPS_SHUTDOWN_DELAY_SECONDS + 15
_CANCEL_FALLBACK_SECONDS = 10
_RECEP_FALLBACK_SECONDS = 15


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
    """Mark the entity as transitioning while a control SET is in flight.

    Behaviour while pending:
    * available = False so the frontend dims/disables the toggle.
    * The coordinator is polled every _POLL_INTERVAL so the UI catches up
      to the actual hardware state without waiting on the 60 s default.
    * The flag is cleared as soon as the polled state matches the
      expected post-action state. A safety-net timeout clears it anyway
      if the UPS never reports the expected state.
    * Concurrent turn_on/turn_off calls are silently dropped.
    """

    _pending: bool = False
    _pending_is_on: bool = False
    _pending_poll_cancel: Any = None
    _pending_timeout_cancel: Any = None

    def _real_is_on(self) -> bool:
        raise NotImplementedError

    def _begin_pending(self, expected_is_on: bool, fallback_seconds: float) -> None:
        self._end_pending()

        self._pending = True
        self._pending_is_on = expected_is_on
        self.async_write_ha_state()

        async def _poll(_now):
            try:
                await self.coordinator.async_refresh()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Pending-state poll failed", exc_info=True)

        self._pending_poll_cancel = async_track_time_interval(
            self.hass, _poll, _POLL_INTERVAL
        )

        @callback
        def _timeout(_now):
            _LOGGER.debug(
                "Pending %s timed out after %.0fs; clearing",
                self.entity_id,
                fallback_seconds,
            )
            self._end_pending()
            self.hass.async_create_task(self.coordinator.async_refresh())

        self._pending_timeout_cancel = async_call_later(
            self.hass, fallback_seconds, _timeout
        )

    def _end_pending(self) -> None:
        if self._pending_poll_cancel is not None:
            self._pending_poll_cancel()
            self._pending_poll_cancel = None
        if self._pending_timeout_cancel is not None:
            self._pending_timeout_cancel()
            self._pending_timeout_cancel = None
        if self._pending:
            self._pending = False
            self.async_write_ha_state()

    def _maybe_clear_pending(self) -> None:
        if self._pending and self._real_is_on() == self._pending_is_on:
            self._end_pending()

    async def async_will_remove_from_hass(self) -> None:
        self._end_pending()
        await super().async_will_remove_from_hass()


class SnmpUpsOutputSwitchEntity(_TransitionMixin, SnmpEntity, SwitchEntity):
    """Switch representing the UPS output as a whole.

    State is derived from xupsOutputSource (534.1.4.5) — the 9SX does not
    implement xupsOutputStatus (534.1.4.10), so the source field is the
    only reliable signal: 2 = none (output off), 3..12 = real source
    (output on).

    Turn-on picks between OffDelay=-1 (cancel a pending shutdown — the
    only case where -1 is accepted by the firmware) and OnDelay=1 (cold
    start; OnDelay=0 is a no-op on this firmware).
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

    def _real_is_on(self) -> bool:
        if self._int(SNMP_OID_CONTROL_OUTPUT_OFF_DELAY) > 0:
            return False
        return self._output_powered()

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return not self._pending

    @property
    def is_on(self) -> bool:
        if self._pending:
            return self._pending_is_on
        return self._real_is_on()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Cancel a pending shutdown or power the output back up."""
        if self._pending:
            return
        if self._int(SNMP_OID_CONTROL_OUTPUT_OFF_DELAY) > 0:
            self._begin_pending(True, _CANCEL_FALLBACK_SECONDS)
            await self.coordinator._api.set(
                [(SNMP_OID_CONTROL_OUTPUT_OFF_DELAY, UPS_SHUTDOWN_CANCEL_VALUE)]
            )
        elif not self._output_powered():
            self._begin_pending(True, _STARTUP_FALLBACK_SECONDS)
            await self.coordinator._api.set(
                [(SNMP_OID_CONTROL_OUTPUT_ON_DELAY, UPS_STARTUP_DELAY_SECONDS)]
            )
        # else: already on, no shutdown pending → no-op

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Initiate a delayed UPS shutdown."""
        if self._pending:
            return
        self._begin_pending(False, _SHUTDOWN_FALLBACK_SECONDS)
        await self.coordinator._api.set(
            [(SNMP_OID_CONTROL_OUTPUT_OFF_DELAY, UPS_SHUTDOWN_DELAY_SECONDS)]
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._maybe_clear_pending()
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

    def _real_is_on(self) -> bool:
        status = self._status()
        if status is None:
            return False
        return status in (ReceptacleStatus.on.value, ReceptacleStatus.pending_off.value)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return not self._pending

    @property
    def is_on(self) -> bool:
        if self._pending:
            return self._pending_is_on
        return self._real_is_on()

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self._pending:
            return
        self._begin_pending(True, _RECEP_FALLBACK_SECONDS)
        await self.coordinator._api.set(
            [(self._on_delay_oid, RECEP_TOGGLE_DELAY_SECONDS)]
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self._pending:
            return
        self._begin_pending(False, _RECEP_FALLBACK_SECONDS)
        await self.coordinator._api.set(
            [(self._off_delay_oid, RECEP_TOGGLE_DELAY_SECONDS)]
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._maybe_clear_pending()
        self.async_write_ha_state()
