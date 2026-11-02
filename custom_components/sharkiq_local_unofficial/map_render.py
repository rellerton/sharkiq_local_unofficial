"""Render a Shark map to PNG in the SharkClean app's live-map design language.

The app (``mapviewv1/MapView.java`` / ``MapPaints.java``) draws the saved map as
vectors: a near-white floor with a soft halo and a thin border, stray wall
fragments as raised grey blobs, room outlines clipped to the floor, and
stadium-shaped room labels; markers keep a fixed size whatever the zoom. This
renders the same layers, in the same order, from the robot's grid. Unlike the
app it also draws while a job runs - the cleaned area, the track and the moving
robot - styled after the app's cleaning-report map.

Sizes follow the app's density-independent pixels. The app fits the floor's
bounding box to 80 % of a ~411 dp wide view, so one dp is the floor's width in
pixels / 329 here. The robot and dock markers are the app's own drawables,
rasterised into ``icons/``; labels use Montserrat Bold (SIL OFL, ``fonts/``) in
place of the app's Gotham Bold.
"""
from __future__ import annotations

import io
import math
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from sharklocal.models import MapGrid, MapPoint, VacuumMap

SCALE = 6  # pixels per grid cell (the basement grid is ~191 x 105 cells)
SUPERSAMPLE = 2  # draw at this multiple, then downsample: anti-aliased vectors
MARGIN_CELLS = 8  # room for the floor's halo at the picture's edge
DP_PER_FLOOR_WIDTH = 1 / 329
# Markers are drawn to the map's own scale, not the app's fixed dp (its map is
# zoomed in; a whole-floor picture at those sizes is ~3.4x life size).
ROBOT_METRES = 0.33  # a Shark lidar robot's diameter (typical; not published per model)
DOCK_METRES = 0.40  # an RV2610 self-empty base: 15.79 in across, per Shark's listing
ROBOT_DISC_FRACTION = 32.1 / 104  # the robot disc's diameter within its drawable
DOCK_BODY_FRACTION = 18 / 36  # the base's width within live_cleaning_robot_dock

ICONS = Path(__file__).parent / "icons"
FONT = Path(__file__).parent / "fonts" / "Montserrat-Bold.ttf"

# Cell values the app counts as floor; wall cells (0x64) sit inside the floor.
FLOOR_CELLS = frozenset({0x00, 0x01, 0x05, 0x0A, 0x0F, 0x19, 0x64})
WALL_CELL = 0x64

# MapPaints colours.
HALO = (0xD6, 0xD6, 0xDB, 255)  # grey_85, three60FillPaint1
FLOOR = (0xF4, 0xF4, 0xF5, 255)  # lightest_gray, three60FillPaint2
BORDER = (0x8A, 0x8B, 0x9C, 255)  # medium_gray
SHRAPNEL = (0xC9, 0xC9, 0xCF, 255)  # grey_80
SHADOW = (0x8A, 0x8B, 0x9C, 255)  # medium_gray, the shrapnel shadow
CLEANED = (0xBB, 0xE5, 0xEE, 255)  # cleaned_area
TRACK = (0xF6, 0xF6, 0xF6, 255)  # grey01, the report map's track
PURPLE = (0x77, 0x00, 0xFF, 255)
PURPLE_FILL = (0x77, 0x00, 0xFF, 0x33)  # purple_translucent_33
SPOT_FILL = (0x77, 0x00, 0xFF, 51)
ROOM_EDGE = (0x8A, 0x8B, 0x9C, 255)
PILL = (0xFC, 0xFC, 0xFF, 255)  # white
PILL_BORDER = (0x19, 0x19, 0x23, 0x16)  # black_translucent
LABEL_TEXT = (0x19, 0x19, 0x23, 255)  # black
TRANSPARENT = (0, 0, 0, 0)


@cache
def _icon(name: str) -> Image.Image:
    return Image.open(ICONS / name).convert("RGBA")


@cache
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT), size)


def render_map(
    vacuum_map: VacuumMap,
    rooms_from: VacuumMap | None = None,
    target: Any = None,
    *,
    docked: bool = False,
    whole_home: bool = False,
) -> bytes:
    """Draw *vacuum_map* and return PNG bytes.

    Live frames carry no rooms, so room names and outlines come from
    *rooms_from* (the latest persisted map) when given. *target* (anything with
    ``rooms`` and ``zone``) is what a running job was sent to clean; without
    one, the saved map's record of the last job is drawn. *docked* draws the
    robot sitting on its dock, as the app does; *whole_home* gives a running whole-home job the
    app's purple look.
    """
    grid = vacuum_map.grid
    source = rooms_from or vacuum_map
    target_rooms, zone = _target(source, target)
    canvas = Canvas.fit(vacuum_map, rooms_from)
    ss = SUPERSAMPLE
    size = (canvas.width * ss, canvas.height * ss)

    def px(point: MapPoint | tuple[float, float]) -> tuple[float, float]:
        x, y = canvas.px(point)
        return (x * ss, y * ss)

    floor_cells, shrapnel_cells, cleaned_cells = _masks(grid)
    offset = (canvas.grid_offset[0] * ss, canvas.grid_offset[1] * ss)
    floor = _place(floor_cells, size, offset, SCALE * ss)
    box = floor.getbbox()
    floor_width = (box[2] - box[0]) / ss if box else canvas.width
    dp = max(1.5, floor_width * DP_PER_FLOOR_WIDTH) * ss

    image = Image.new("RGBA", size, TRANSPARENT)

    # 1-2. Floor: the halo (half of a 22 dp stroke), then the fill.
    image.paste(HALO, (0, 0), _dilate(floor, round(11 * dp)))
    image.paste(FLOOR, (0, 0), floor)
    if whole_home:
        _paste_clipped(image, _solid(size, PURPLE_FILL), floor)

    # 3. Wall fragments ("shrapnel"): raised grey blobs with a hard shadow.
    shrapnel = _dilate(_place(shrapnel_cells, size, offset, SCALE * ss), round(0.5 * dp))
    shadow = ImageChops.offset(shrapnel, 0, round(3.3 * dp))
    image.paste(SHADOW, (0, 0), ImageChops.multiply(shadow, floor))
    image.paste(SHRAPNEL, (0, 0), ImageChops.multiply(shrapnel, floor))

    # 4. What the job cleaned, then its track, inside the floor.
    cleaned = _dilate(_place(cleaned_cells, size, offset, SCALE * ss), SCALE * ss // 2)
    image.paste(CLEANED, (0, 0), ImageChops.multiply(cleaned, floor))
    if len(vacuum_map.path) >= 2:
        track = Image.new("RGBA", size, TRANSPARENT)
        ImageDraw.Draw(track).line(
            [px(p) for p in vacuum_map.path], fill=TRACK, width=max(1, round(1 * dp))
        )
        _paste_clipped(image, track, floor)

    # 5. The floor border, 2 dp centred on its edge.
    edge = ImageChops.subtract(_dilate(floor, round(dp)), _erode(floor, round(dp)))
    image.paste(PURPLE if whole_home else BORDER, (0, 0), edge)

    # 6. Rooms: every room outlined, picked ones filled; clipped to the floor.
    rooms = Image.new("RGBA", size, TRANSPARENT)
    draw = ImageDraw.Draw(rooms)
    for room in source.named_rooms:
        if len(room.polygon) < 3:
            continue
        points = [px(p) for p in room.polygon]
        picked = room.name in target_rooms
        if picked:
            draw.polygon(points, fill=PURPLE_FILL)
        draw.line(
            points + points[:1],
            fill=PURPLE if picked or whole_home else ROOM_EDGE,
            width=max(1, round(2 * dp)),
            joint="curve",
        )
    _paste_clipped(image, rooms, floor)

    # 7. Labels, on each room's bounding-box centre.
    _labels(image, source, px, dp, target_rooms, whole_home)

    # 8. The spot zone.
    if len(zone) >= 3:
        _spot_zone(image, [px(p) for p in zone], dp)

    # 9-10. Dock base, then the robot over it. Docked, the robot sits on its
    # dock facing out, as in the app; the dock pose is where it sits then.
    px_per_metre = SCALE * ss / grid.resolution
    robot_size = round(ROBOT_METRES * px_per_metre / ROBOT_DISC_FRACTION)
    disc_radius = ROBOT_METRES * px_per_metre / 2
    dock = source.dock
    if dock is not None:
        # The base proper is only 18 of the drawable's 36 units wide.
        width = round(DOCK_METRES * px_per_metre / DOCK_BODY_FRACTION)
        base = _icon("dock_base.png").resize((width, round(width * 20 / 36)), Image.Resampling.LANCZOS)
        # The base's front edge faces up in the drawable; turn it to face the
        # way the robot leaves, and set it behind the robot: Shark robots
        # charge through contacts at the back (the log's CHARGE_BACK).
        base = base.rotate(math.degrees(dock.heading) - 90, expand=True, resample=Image.Resampling.BICUBIC)
        x, y = px((dock.x, dock.y))
        behind = disc_radius * 0.9
        _paste_centred(
            image,
            base,
            (x - math.cos(dock.heading) * behind, y + math.sin(dock.heading) * behind),
        )
    pose = (dock.x, dock.y, dock.heading) if docked and dock is not None else None
    if pose is None and (robot := vacuum_map.robot) is not None:
        pose = (robot.x, robot.y, robot.heading)
    if pose is not None:
        marker = _icon("robot.png").resize((robot_size, robot_size), Image.Resampling.LANCZOS)
        # The drawable faces -x; headings are anticlockwise from +x, as PIL rotates.
        marker = marker.rotate(math.degrees(pose[2]) - 180, resample=Image.Resampling.BICUBIC)
        _paste_centred(image, marker, px((pose[0], pose[1])))

    image = image.resize((canvas.width, canvas.height), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Geometry shared with the map card calibration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Canvas:
    """Where the map sits in the picture: the world-to-pixel transform.

    It depends only on the map (the grid and the room outlines, which can reach
    past the explored grid), never on a job's target, so the calibration a map
    card reads stays true for the picture it is showing.
    """

    origin_x: float  # world x of the grid's cell (0, 0)
    origin_y: float
    resolution: float
    grid_height: int
    cols_left: int  # cells of margin left of the grid
    rows_above: int  # cells of margin above the grid
    width: int  # pixels
    height: int

    @classmethod
    def fit(cls, vacuum_map: VacuumMap, rooms_from: VacuumMap | None = None) -> Canvas:
        """The canvas for *vacuum_map* with *rooms_from*'s room outlines."""
        grid = vacuum_map.grid
        res = grid.resolution
        points = [p for room in (rooms_from or vacuum_map).named_rooms for p in room.polygon]
        grid_right = grid.origin.x + grid.width * res
        grid_top = grid.origin.y + grid.height * res
        cols_left = MARGIN_CELLS + max(
            0, math.ceil((grid.origin.x - min([grid.origin.x, *(p.x for p in points)])) / res)
        )
        cols_right = MARGIN_CELLS + max(
            0, math.ceil((max([grid_right, *(p.x for p in points)]) - grid_right) / res)
        )
        rows_above = MARGIN_CELLS + max(
            0, math.ceil((max([grid_top, *(p.y for p in points)]) - grid_top) / res)
        )
        rows_below = MARGIN_CELLS + max(
            0, math.ceil((grid.origin.y - min([grid.origin.y, *(p.y for p in points)])) / res)
        )
        return cls(
            origin_x=grid.origin.x,
            origin_y=grid.origin.y,
            resolution=res,
            grid_height=grid.height,
            cols_left=cols_left,
            rows_above=rows_above,
            width=(cols_left + grid.width + cols_right) * SCALE,
            height=(rows_above + grid.height + rows_below) * SCALE,
        )

    @property
    def grid_offset(self) -> tuple[int, int]:
        """Pixel position of the grid's top-left corner."""
        return (self.cols_left * SCALE, self.rows_above * SCALE)

    def px(self, point: MapPoint | tuple[float, float]) -> tuple[float, float]:
        """World metres to picture pixels."""
        x, y = (point.x, point.y) if isinstance(point, MapPoint) else point
        col = (x - self.origin_x) / self.resolution
        row = (y - self.origin_y) / self.resolution
        # Row 0 is the bottom of the map; image row 0 is the top.
        return (
            (self.cols_left + col) * SCALE,
            (self.rows_above + self.grid_height - row) * SCALE,
        )

    def calibration_points(self) -> list[dict[str, dict[str, float]]]:
        """Three world/pixel pairs, in the shape vacuum map cards read."""
        corners = [
            (self.origin_x, self.origin_y),
            (self.origin_x + 1.0, self.origin_y),
            (self.origin_x, self.origin_y + 1.0),
        ]
        points = []
        for x, y in corners:
            map_x, map_y = self.px((x, y))
            points.append(
                {
                    "vacuum": {"x": round(x, 4), "y": round(y, 4)},
                    "map": {"x": round(map_x, 2), "y": round(map_y, 2)},
                }
            )
        return points


def _target(source: VacuumMap, target: Any) -> tuple[set[str], Sequence[MapPoint]]:
    """The rooms and zone to highlight."""
    if target is not None:
        return set(target.rooms), target.zone
    spot = source.spot
    zone = spot.polygon if spot is not None and spot.selected else ()
    return {room.name for room in source.named_rooms if room.selected}, zone


# ---------------------------------------------------------------------------
# Masks from the grid
# ---------------------------------------------------------------------------


def _masks(grid: MapGrid) -> tuple[Image.Image, Image.Image, Image.Image]:
    """Floor (holes filled), wall fragments and cleaned cells, one pixel per cell.

    Row 0 of the grid is the bottom of the map, so rows are flipped here.
    """
    w, h = grid.width, grid.height
    floor = Image.new("L", (w, h), 0)
    cleaned = Image.new("L", (w, h), 0)
    walls: set[tuple[int, int]] = set()
    floor_px, cleaned_px = floor.load(), cleaned.load()
    for row in range(h):
        y = h - 1 - row
        for col in range(w):
            value = grid.cell(col, row)
            if value in FLOOR_CELLS:
                floor_px[col, y] = 255
                if value == WALL_CELL:
                    walls.add((col, y))
                elif MapGrid.is_cleaned(value):
                    cleaned_px[col, y] = 255
    floor = _fill_holes(floor)

    # The wall component with the largest bounding box is the floor's outer
    # ring and is simply floor; every other one is a fragment.
    shrapnel = Image.new("L", (w, h), 0)
    components = _components(walls)
    if len(components) > 1:
        components.sort(key=_bbox_area)
        shrapnel_px = shrapnel.load()
        for component in components[:-1]:
            for x, y in component:
                shrapnel_px[x, y] = 255
    return floor, shrapnel, cleaned


def _fill_holes(mask: Image.Image) -> Image.Image:
    """The app traces only the floor's outer contour: fill anything enclosed."""
    padded = Image.new("L", (mask.width + 2, mask.height + 2), 0)
    padded.paste(mask, (1, 1))
    ImageDraw.floodfill(padded, (0, 0), 128)
    outside = padded.point(lambda v: 255 if v == 128 else 0)
    return ImageChops.invert(outside).crop((1, 1, mask.width + 1, mask.height + 1))


def _components(cells: set[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    """8-connected components."""
    seen: set[tuple[int, int]] = set()
    out = []
    for start in cells:
        if start in seen:
            continue
        seen.add(start)
        queue, component = deque([start]), []
        while queue:
            x, y = queue.popleft()
            component.append((x, y))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    n = (x + dx, y + dy)
                    if n in cells and n not in seen:
                        seen.add(n)
                        queue.append(n)
        out.append(component)
    return out


def _bbox_area(component: list[tuple[int, int]]) -> int:
    xs = [x for x, _ in component]
    ys = [y for _, y in component]
    return (max(xs) - min(xs) + 1) * (max(ys) - min(ys) + 1)


def _place(cells: Image.Image, size: tuple[int, int], offset: tuple[int, int], scale: int) -> Image.Image:
    """A cell mask scaled up into the full picture."""
    out = Image.new("L", size, 0)
    out.paste(cells.resize((cells.width * scale, cells.height * scale), Image.Resampling.NEAREST), offset)
    return out


def _dilate(mask: Image.Image, radius: int) -> Image.Image:
    """Grow *mask* by *radius* pixels (a square structuring element)."""
    return mask.filter(ImageFilter.BoxBlur(radius)).point(lambda v: 255 if v else 0)


def _erode(mask: Image.Image, radius: int) -> Image.Image:
    return ImageChops.invert(_dilate(ImageChops.invert(mask), radius))


def _solid(size: tuple[int, int], colour: tuple[int, int, int, int]) -> Image.Image:
    return Image.new("RGBA", size, colour)


def _paste_clipped(image: Image.Image, layer: Image.Image, clip: Image.Image) -> None:
    """Composite *layer* over *image*, only where *clip* is set."""
    layer.putalpha(ImageChops.multiply(layer.getchannel("A"), clip))
    image.alpha_composite(layer)


def _paste_centred(image: Image.Image, marker: Image.Image, centre: tuple[float, float]) -> None:
    """Draw *marker* centred on *centre*, blended by its own alpha, clipped at the edges."""
    layer = Image.new("RGBA", image.size, TRANSPARENT)
    layer.paste(marker, (round(centre[0] - marker.width / 2), round(centre[1] - marker.height / 2)))
    image.alpha_composite(layer)


# ---------------------------------------------------------------------------
# Labels and the spot zone
# ---------------------------------------------------------------------------


def _labels(image, source, px, dp: float, target_rooms: set[str], whole_home: bool) -> None:
    """The app's stadium pills, fitted to rooms without covering each other."""
    draw = ImageDraw.Draw(image)
    for label in _label_layout(source, px, dp, image.size, target_rooms, whole_home):
        font = _font(label.font_size)
        draw.rounded_rectangle(
            label.box,
            radius=(label.box[3] - label.box[1]) / 2,
            fill=PURPLE if label.picked else PILL,
            outline=PURPLE if label.picked else PILL_BORDER,
            width=max(1, round(dp)),
        )
        draw.text(
            label.center,
            label.name,
            font=font,
            fill=PILL if label.picked else LABEL_TEXT,
            anchor="mm",
        )


@dataclass(frozen=True)
class _MapLabel:
    name: str
    center: tuple[float, float]
    box: tuple[float, float, float, float]
    font_size: int
    picked: bool


def _label_layout(
    source: VacuumMap,
    px: Any,
    dp: float,
    image_size: tuple[int, int],
    target_rooms: set[str],
    whole_home: bool,
) -> list[_MapLabel]:
    """Fit labels to their rooms and choose positions with minimal overlap."""
    measure = ImageDraw.Draw(Image.new("L", (1, 1)))
    image_width, image_height = image_size
    placed: list[tuple[float, float, float, float]] = []
    labels: list[_MapLabel] = []

    for room in source.named_rooms:
        name = room.name.strip()
        if not name or not room.polygon:
            continue
        points = [px(point) for point in room.polygon]
        left, right = min(x for x, _ in points), max(x for x, _ in points)
        top, bottom = min(y for _, y in points), max(y for _, y in points)
        room_width, room_height = right - left, bottom - top
        max_width = max(32.0, min(image_width - 4.0, room_width * 0.78))
        max_height = max(18.0, min(room_height * 0.30, 17.0 * dp))
        font_size = max(8, round(8.5 * dp))
        minimum = max(8, round(3.2 * dp))

        while font_size > minimum:
            font = _font(font_size)
            text_width = measure.textlength(name, font=font)
            pill_width = text_width * 1.08 + 8 * dp
            pill_height = 1.43 * font_size
            if pill_width <= max_width and pill_height <= max_height:
                break
            font_size -= 1

        font = _font(font_size)
        text_width = measure.textlength(name, font=font)
        pill_width = min(max_width, text_width * 1.08 + 8 * dp)
        pill_height = min(max_height, 1.43 * font_size)
        half_width, half_height = pill_width / 2, pill_height / 2
        cx, cy = (left + right) / 2, (top + bottom) / 2
        candidates = (
            (cx, cy),
            (cx, top + room_height * 0.30),
            (cx, top + room_height * 0.70),
            (left + room_width * 0.30, cy),
            (left + room_width * 0.70, cy),
        )

        choices: list[tuple[float, tuple[float, float], tuple[float, float, float, float]]] = []
        for candidate_x, candidate_y in candidates:
            candidate_x = min(
                max(candidate_x, max(left + half_width, half_width + 2)),
                min(right - half_width, image_width - half_width - 2),
            )
            candidate_y = min(
                max(candidate_y, max(top + half_height, half_height + 2)),
                min(bottom - half_height, image_height - half_height - 2),
            )
            box = (
                candidate_x - half_width,
                candidate_y - half_height,
                candidate_x + half_width,
                candidate_y + half_height,
            )
            overlap = sum(_box_overlap(box, other) for other in placed)
            choices.append((overlap, (candidate_x, candidate_y), box))

        _, center, box = min(choices, key=lambda choice: choice[0])
        placed.append(box)
        labels.append(
            _MapLabel(
                name=name,
                center=center,
                box=box,
                font_size=font_size,
                picked=room.name in target_rooms or whole_home,
            )
        )
    return labels


def _box_overlap(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    """Area shared by two boxes, zero when they do not intersect."""
    width = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    height = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    return width * height


def _spot_zone(image: Image.Image, points: list[tuple[float, float]], dp: float) -> None:
    """SpotCleanMovingDrawable: fill, 8 dashes a side, corner brackets, ring, dot."""
    layer = Image.new("RGBA", image.size, TRANSPARENT)
    draw = ImageDraw.Draw(layer)
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
    side = max(right - left, bottom - top)
    cx, cy = (left + right) / 2, (top + bottom) / 2
    stroke = max(1, round(0.016 * side))

    ring = 0.16 * side
    draw.ellipse((cx - ring, cy - ring, cx + ring, cy + ring), outline=PURPLE, width=stroke)
    draw.rounded_rectangle((left, top, right, bottom), radius=0.016 * side, fill=SPOT_FILL)
    dash, gap = 0.05 * side, 0.075 * side
    for (x1, y1), (x2, y2) in (
        ((left, top), (right, top)),
        ((right, top), (right, bottom)),
        ((right, bottom), (left, bottom)),
        ((left, bottom), (left, top)),
    ):
        length = math.hypot(x2 - x1, y2 - y1)
        ux, uy = (x2 - x1) / length, (y2 - y1) / length
        at = 0.0
        while at < length:
            end = min(at + dash, length)
            draw.line([(x1 + ux * at, y1 + uy * at), (x1 + ux * end, y1 + uy * end)], fill=PURPLE, width=stroke)
            at += dash + gap
    leg, width = 0.075 * side, max(1, round(0.018 * side))
    for x, y, sx, sy in ((left, top, 1, 1), (right, top, -1, 1), (right, bottom, -1, -1), (left, bottom, 1, -1)):
        draw.line([(x + sx * leg, y), (x, y), (x, y + sy * leg)], fill=PURPLE, width=width, joint="curve")
    dot = 0.048 * side
    draw.ellipse((cx - dot, cy - dot, cx + dot, cy + dot), fill=PURPLE)
    image.alpha_composite(layer)
