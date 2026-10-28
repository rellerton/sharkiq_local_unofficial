"""Camera platform for Shark IQ (Local): the live map."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from sharklocal.models import VacuumMap, VacuumMode

from .const import CONF_NAME, DOMAIN
from .coordinator import JobTarget, SharkCoordinator
from .entity import SharkBaseEntity
from .map_render import Canvas, render_map

MAP_FRAME_INTERVAL = 2.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the map camera (MQTT only: maps are never served over REST)."""
    coordinator: SharkCoordinator = hass.data[DOMAIN][entry.entry_id]
    if coordinator.use_mqtt and coordinator.capabilities.has_map:
        async_add_entities([SharkMapCamera(coordinator, entry.data[CONF_NAME])])


class SharkMapCamera(SharkBaseEntity, Camera):
    """The map as the robot last published it, with the path cleaned so far.

    The camera's stable ``idle`` state avoids recording every map frame. A live
    camera viewer receives an MJPEG still-image stream at the robot's normal map
    cadence; a still-image request receives the most recent rendered map.
    """

    _attr_frame_interval = MAP_FRAME_INTERVAL
    _attr_translation_key = "map"

    def __init__(self, coordinator: SharkCoordinator, entry_title: str) -> None:
        """Initialize."""
        SharkBaseEntity.__init__(self, coordinator, entry_title)
        Camera.__init__(self)
        self.content_type = "image/png"
        self._attr_unique_id = f"{coordinator.unique_id}_map"
        self._drawn: _Picture | None = None
        self._png: bytes | None = None
        self._track_map()

    def _current(self) -> _Picture | None:
        data = self.coordinator.data
        if data is None or data.map is None:
            return None
        status = data.status
        target = self.coordinator.job_target
        return _Picture(
            data.map,
            data.persisted_map,
            target,
            # The app hides the robot on its dock, and shows a whole-home job
            # (one with no rooms or zone) in purple.
            docked=status.is_docked,
            whole_home=status.mode == VacuumMode.CLEANING and target is None,
        )

    @callback
    def _track_map(self) -> None:
        # Status frames arrive every few seconds; only a new map object (or a
        # new job target) is a new picture, and rendering waits until something
        # asks for it.
        current = self._current()
        if current is not None and (
            self._drawn is None or not current.same_as(self._drawn)
        ):
            self._drawn = current
            self._png = None

    @callback
    def _handle_coordinator_update(self) -> None:
        self._track_map()
        super()._handle_coordinator_update()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Calibration for vacuum map cards: map metres to picture pixels."""
        if self._drawn is None:
            return None
        canvas = Canvas.fit(self._drawn.vacuum_map, self._drawn.rooms_from)
        return {"calibration_points": canvas.calibration_points()}

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the rendered map, drawing it on first request."""
        if self._drawn is None:
            return None
        if self._png is None:
            drawn = self._drawn
            png = await self.hass.async_add_executor_job(drawn.render)
            if drawn is self._drawn:
                self._png = png
            return png
        return self._png


@dataclass(frozen=True)
class _Picture:
    """Everything the picture depends on."""

    vacuum_map: VacuumMap
    rooms_from: VacuumMap | None
    target: JobTarget | None
    docked: bool
    whole_home: bool

    def same_as(self, other: _Picture) -> bool:
        # Maps compare by identity: a new frame is a new object, and comparing
        # the rasters themselves every few seconds would be wasted work.
        return (
            self.vacuum_map is other.vacuum_map
            and self.rooms_from is other.rooms_from
            and (self.target, self.docked, self.whole_home)
            == (other.target, other.docked, other.whole_home)
        )

    def render(self) -> bytes:
        return render_map(
            self.vacuum_map,
            self.rooms_from,
            self.target,
            docked=self.docked,
            whole_home=self.whole_home,
        )
