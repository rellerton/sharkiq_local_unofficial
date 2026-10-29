"""Sensor platform for Shark IQ (Local)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfArea,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from sharklocal import codes as robot_codes
from sharklocal.models import SPOT_ROOM_NAME, VacuumMap

from .codes import advice, describe, end_reason
from .const import CONF_NAME, DOMAIN
from .coordinator import SharkCoordinator, SharkData
from .entity import SharkBaseEntity


@dataclass(frozen=True, kw_only=True)
class SharkSensorDescription(SensorEntityDescription):
    """Describes a Shark sensor."""

    value_fn: Callable[[SharkData], Any]
    attrs_fn: Callable[[SharkData], dict[str, Any] | None] | None = None
    mqtt_only: bool = False
    # From the REST wifi_status endpoint alone: MQTT status frames carry no
    # signal level, SSID or address (PbDeviceInfo has fields for the signal,
    # but the robot has not been seen to send them).
    rest_only: bool = False


def _last_job(data: SharkData) -> VacuumMap | None:
    return data.persisted_map


def _job_started(data: SharkData) -> datetime | None:
    job = _last_job(data)
    if job is None or not job.job_started:
        return None
    return datetime.fromtimestamp(job.job_started, UTC)


def _log_codes(data: SharkData, key: str) -> list[str] | None:
    """Codes logged under *key* in the last job's event log, in log order.

    The robot's clock can jump mid-job, so log order is the only reliable
    order. None when no persisted map has been seen yet, or when it is one
    sent on request, which has no event log: that says nothing about what the
    job logged.
    """
    job = _last_job(data)
    if job is None:
        return None
    codes = [entry.code for entry in job.log if entry.key == key]
    # A normal map returned on request has no per-job event entries. Some
    # firmware, however, returns the previous job's complete log while leaving
    # the report flag clear. Treat only an actually present key as evidence in
    # that case; absence still means unknown rather than "none".
    if not job.report and not codes:
        return None
    return codes


def _last_code(data: SharkData, key: str) -> str | None:
    """The last code logged under *key*; "none" if the job logged none."""
    codes = _log_codes(data, key)
    if codes is None:
        return None
    if not codes:
        return "none"
    return end_reason(codes[-1]) if key == "DT_WFF_TERM_CODE" else codes[-1]


def _job_outcome_code(data: SharkData) -> str | None:
    """Return the whole-job outcome, preferring a later terminal interruption.

    ``DT_WFF_TERM_CODE`` is emitted once per completed room/zone.  Its final
    ``NORMAL`` value therefore does not prove that the whole job completed.  A
    Recharge & Resume interruption is logged later when dock servicing aborts
    the pending continuation, and must take precedence.
    """
    job = _last_job(data)
    if job is None:
        return None
    if (
        _log_codes(data, "DT_WFF_TERM_CODE") is None
        and _log_codes(data, "DT_2R_INTERUPT_CODE") is None
    ):
        return None

    outcome: str | None = None
    for entry in job.log:
        if entry.key == "DT_WFF_TERM_CODE":
            outcome = end_reason(entry.code)
        elif entry.key == "DT_2R_INTERUPT_CODE":
            outcome = entry.code
    return outcome or "none"


def _job_outcome_attrs(data: SharkData) -> dict[str, Any]:
    code = _job_outcome_code(data)
    attrs: dict[str, Any] = {"code": code}
    if code and code.startswith("RR_INT_"):
        error_code = _last_code(data, "DT_ERROR_CODE")
        attrs.update(
            {
                "error_code": error_code,
                "error": describe(error_code),
                "advice": advice(error_code),
            }
        )
    return attrs


def _clean_mode(data: SharkData) -> str | None:
    """The last job's cleaning mode code: "Cleaning mode: ROOM_SELECTION" -> ROOM_SELECTION.

    The robot logs a mode for some room selections only, and a spot clean as
    one of those (of its saved spot zone) when it logs one at all, so the
    report's selected rooms decide (measured 2026-09-27): the spot zone alone
    is a spot clean, logged or not; none selected is a whole-home job. SPOT
    and WHOLE_HOME are this integration's names, not codes the robot sends.
    """
    codes = _log_codes(data, "DT_CLEANING_MODE")
    if codes is None:
        return None
    job = data.persisted_map
    selected = [room for room in job.rooms if room.selected] if job else []
    if selected and all(room.name == SPOT_ROOM_NAME for room in selected):
        return "SPOT"
    if codes:
        return codes[-1].split(":", 1)[-1].strip()
    return "WHOLE_HOME" if job and job.rooms and not selected else "none"


def _log_numbers(job: VacuumMap, predicate: Callable[[str], bool]) -> list[int]:
    return [int(e.code) for e in job.log if predicate(e.key) and e.code.lstrip("-").isdigit()]


def _job_battery(job: VacuumMap) -> tuple[int | None, int | None]:
    """Return the battery at the real job start and at its final transition.

    A completed report can begin with a short boot-relative diagnostic block.
    On the RV2500AA that block contains a placeholder battery value of zero,
    before the same log records the actual epoch-dated cleaning transition and
    its 100 percent battery value.  Prefer the battery paired with that
    epoch-dated transition; older reports without one retain the first usable
    positive battery value as their fallback.  The last transition remains the
    best end-of-job reading for every termination path.
    """
    readings: list[tuple[int, int]] = []
    for index, entry in enumerate(job.log):
        if (
            entry.key == "DT_BATTERY_VALUE_AT_STATE_TRANS"
            and entry.code.isdigit()
            and 0 <= (value := int(entry.code)) <= 100
        ):
            readings.append((index, value))

    if not readings:
        return None, None

    start: int | None = None
    for index, entry in enumerate(job.log):
        if (
            entry.key == "DT_STATE_CHANGE"
            and entry.code == "SYS_ST_CLEANING"
            and entry.time >= 1_000_000_000
        ):
            start = next((value for at, value in readings if at > index), None)
            if start is not None:
                break

    if start is None:
        start = next((value for _, value in readings if value > 0), readings[0][1])
    return start, readings[-1][1]


def _job_details(data: SharkData) -> dict[str, Any] | None:
    """What the robot's log says about the last job, beyond area and time."""
    if (job := _last_job(data)) is None:
        return None
    battery_start, battery_end = _job_battery(job)
    return {
        "code": _clean_mode(data),
        "battery_start": battery_start,
        "battery_end": battery_end,
        "bumper_hits": sum(_log_numbers(job, lambda k: k.startswith("DT_BUMPER_") and k.endswith("_COUNTER"))),
        "cliff_events": sum(_log_numbers(job, lambda k: k.startswith("DT_CLIFF_") and k.endswith("_COUNTER"))),
        "obstacles_avoided": sum(_log_numbers(job, lambda k: k.startswith("DT_IR_AVOID_") and k.endswith("_COUNTER"))),
        "docking_time": sum(_log_numbers(job, lambda k: k.endswith("_DOCK_TIME") and k.startswith("DT_")
                                         and k not in ("DT_TIME_BASE_DOCK", "DT_TIME_PP_DOCK"))),
    }


def _code_attrs(data: SharkData, key: str) -> dict[str, Any]:
    # The raw code is the stable thing for automations to match on.
    return {"code": _last_code(data, key)}


def _warning_attrs(data: SharkData) -> dict[str, Any]:
    codes = _log_codes(data, "DT_WARNING_CODE")
    return {
        **_code_attrs(data, "DT_WARNING_CODE"),
        "warnings": None if codes is None else [describe(code) for code in codes],
        "warning_codes": codes,
    }


def _live(codes: list[int] | None, table: dict[int, str]) -> list[str] | None:
    """Names of the codes active now, zero ("none") dropped, each once."""
    if codes is None:
        return None
    return list(dict.fromkeys(robot_codes.names(table, [c for c in codes if c])))


def _live_errors(data: SharkData) -> list[str] | None:
    return _live(data.status.errors, robot_codes.ERROR_CODES)


def _live_warnings(data: SharkData) -> list[str] | None:
    return _live(data.status.warnings, robot_codes.WARNING_CODES)


def _first_described(names: list[str] | None) -> str | None:
    if names is None:
        return None
    return describe(names[0]) if names else "None"


def _error_attrs(data: SharkData) -> dict[str, Any]:
    names = _live_errors(data) or []
    return {
        "code": names[0] if names else "ERROR_NONE",
        "codes": names,
        "errors": [describe(name) for name in names],
        "advice": advice(names[0]) if names else None,
    }


def _live_warning_attrs(data: SharkData) -> dict[str, Any]:
    names = _live_warnings(data) or []
    return {
        "code": names[0] if names else "WARN_NONE",
        "codes": names,
        "warnings": [describe(name) for name in names],
    }


def _named(value: int | None, table: dict[int, str]) -> str | None:
    if value is None:
        return None
    return table.get(value, f"UNKNOWN_{value}")


SENSORS: tuple[SharkSensorDescription, ...] = (
    SharkSensorDescription(
        key="battery",
        translation_key="battery",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda data: data.status.battery_level,
    ),
    SharkSensorDescription(
        key="last_clean_area",
        translation_key="last_clean_area",
        device_class=SensorDeviceClass.AREA,
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        suggested_display_precision=1,
        mqtt_only=True,
        value_fn=lambda data: (job := _last_job(data)) and job.cleaned_area,
    ),
    SharkSensorDescription(
        key="last_clean_duration",
        translation_key="last_clean_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        mqtt_only=True,
        value_fn=lambda data: (job := _last_job(data)) and job.clean_time,
    ),
    SharkSensorDescription(
        key="last_clean_start",
        translation_key="last_clean_start",
        device_class=SensorDeviceClass.TIMESTAMP,
        mqtt_only=True,
        value_fn=_job_started,
    ),
    SharkSensorDescription(
        key="last_clean_mode",
        translation_key="last_clean_mode",
        mqtt_only=True,
        value_fn=lambda data: describe(_clean_mode(data)),
        attrs_fn=_job_details,
    ),
    # The robot's own event log arrives with the persisted map at the end of
    # every job; these are its fault and job-outcome codes.
    SharkSensorDescription(
        key="last_warning",
        translation_key="last_warning",
        entity_category=EntityCategory.DIAGNOSTIC,
        mqtt_only=True,
        value_fn=lambda data: describe(_last_code(data, "DT_WARNING_CODE")),
        attrs_fn=_warning_attrs,
    ),
    SharkSensorDescription(
        key="last_end_reason",
        translation_key="last_end_reason",
        entity_category=EntityCategory.DIAGNOSTIC,
        mqtt_only=True,
        value_fn=lambda data: describe(_job_outcome_code(data)),
        attrs_fn=_job_outcome_attrs,
    ),
    SharkSensorDescription(
        key="last_dock_reason",
        translation_key="last_dock_reason",
        entity_category=EntityCategory.DIAGNOSTIC,
        mqtt_only=True,
        value_fn=lambda data: describe(_last_code(data, "DT_DOCK_CODE")),
        attrs_fn=lambda data: _code_attrs(data, "DT_DOCK_CODE"),
    ),
    # Live, from every status frame.
    SharkSensorDescription(
        key="error",
        translation_key="error",
        mqtt_only=True,
        value_fn=lambda data: _first_described(_live_errors(data)),
        attrs_fn=_error_attrs,
    ),
    SharkSensorDescription(
        key="warning",
        translation_key="warning",
        entity_category=EntityCategory.DIAGNOSTIC,
        mqtt_only=True,
        value_fn=lambda data: _first_described(_live_warnings(data)),
        attrs_fn=_live_warning_attrs,
    ),
    SharkSensorDescription(
        key="robot_state",
        translation_key="robot_state",
        entity_category=EntityCategory.DIAGNOSTIC,
        mqtt_only=True,
        value_fn=lambda data: describe(_named(data.status.state, robot_codes.SYSTEM_STATES)),
        attrs_fn=lambda data: {"code": _named(data.status.state, robot_codes.SYSTEM_STATES)},
    ),
    SharkSensorDescription(
        key="temperature",
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        # An RV2610 reports exactly 20 in every frame, docked or cleaning, and
        # the app never reads the field, so it is off until a model shows it
        # moving.
        entity_registry_enabled_default=False,
        mqtt_only=True,
        value_fn=lambda data: data.status.temperature,
    ),
    SharkSensorDescription(
        key="relocation",
        translation_key="relocation",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        mqtt_only=True,
        value_fn=lambda data: describe(_named(data.status.relocation, robot_codes.RELOCATION_STATES)),
        attrs_fn=lambda data: {"code": _named(data.status.relocation, robot_codes.RELOCATION_STATES)},
    ),
    *(
        SharkSensorDescription(
            key=key,
            translation_key=key,
            # Percent of full power: suction reads 50 on eco and 100 on max,
            # never more.
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            mqtt_only=True,
            value_fn=lambda data, attr=attr: getattr(data.status, attr),
        )
        for key, attr in (
            ("suction_motor_speed", "fan_speed"),
            ("brushroll_speed", "brushroll_speed"),
            ("side_brush_speed", "side_brush_speed"),
        )
    ),
    SharkSensorDescription(
        key="rssi",
        translation_key="rssi",
        rest_only=True,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.wifi.rssi if data.wifi else None,
    ),
    SharkSensorDescription(
        key="ssid",
        translation_key="ssid",
        rest_only=True,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.wifi.ssid if data.wifi else None,
    ),
    SharkSensorDescription(
        key="ip_address",
        translation_key="ip_address",
        rest_only=True,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.wifi.ip_address if data.wifi else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors."""
    coordinator: SharkCoordinator = hass.data[DOMAIN][entry.entry_id]
    name = entry.data[CONF_NAME]
    async_add_entities(
        SharkSensor(coordinator, name, desc)
        for desc in SENSORS
        if (coordinator.use_mqtt or not desc.mqtt_only)
        # Firmware whose REST endpoints are dead would leave these unknown
        # for good.
        and (coordinator.has_wifi_status or not desc.rest_only)
    )


class SharkSensor(SharkBaseEntity, SensorEntity):
    """A sensor reading derived from the coordinator data."""

    entity_description: SharkSensorDescription

    def __init__(
        self,
        coordinator: SharkCoordinator,
        entry_title: str,
        description: SharkSensorDescription,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, entry_title)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.unique_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        """Return the value."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes, if the description defines any."""
        if self.coordinator.data is None or self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)
