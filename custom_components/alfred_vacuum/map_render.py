"""Disegno della mappa e dati per la card interattiva.

Coordinate:
  - robot (mm): quelle dei comandi e dei dati del robot
  - cella:      (x - sinistra) / dimensione cella
  - immagine:   cella * SCALE, con l'asse verticale capovolto (nel robot
                l'asse y cresce verso l'alto, nelle immagini verso il basso)

La card "Xiaomi Vacuum Map Card" usa i 'calibration_points' per convertire
i tocchi sull'immagine in coordinate del robot.
"""

from __future__ import annotations

import io
import math
from collections import Counter
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .map_decoder import MapData, Room

SCALE = 4
WALL_VALUE = 63
BACKGROUND = (0, 0, 0, 0)
WALL = (90, 96, 105, 255)
FLOOR = (200, 204, 210, 255)
CHARGER = (46, 160, 67, 255)
ROBOT = (255, 255, 255, 255)
ROBOT_EDGE = (30, 30, 30, 255)
TEXT = (20, 20, 20, 255)
TEXT_EDGE = (255, 255, 255, 230)
HIGHLIGHT = (255, 140, 0, 255)
ZONE_FILL = (255, 140, 0, 90)
ROOM_COLORS = [
    (125, 179, 232), (247, 178, 103), (140, 205, 140), (230, 140, 160),
    (190, 160, 230), (240, 214, 110), (120, 200, 200), (210, 170, 140),
]


class MapGeometry:
    """Conversioni tra coordinate del robot e dell'immagine."""

    def __init__(self, data: MapData) -> None:
        self.d = data
        self.width = data.width * SCALE
        self.height = data.height * SCALE

    def to_image(self, x: float, y: float) -> tuple[float, float]:
        col = (x - self.d.left) / self.d.grid
        row = (y - self.d.top) / self.d.grid
        return col * SCALE, (self.d.height - row) * SCALE

    def to_robot(self, px: float, py: float) -> tuple[int, int]:
        col = px / SCALE
        row = self.d.height - py / SCALE
        return round(self.d.left + col * self.d.grid), round(self.d.top + row * self.d.grid)

    def calibration_points(self) -> list[dict[str, dict[str, float]]]:
        points = []
        for px, py in ((0, 0), (self.width, 0), (0, self.height)):
            rx, ry = self.to_robot(px, py)
            points.append({"vacuum": {"x": rx, "y": ry}, "map": {"x": px, "y": py}})
        return points


def pixel_histogram(data: MapData, top: int = 12) -> dict[int, int]:
    """Valori dei pixel più frequenti (per capire il formato in diagnostica)."""
    return dict(Counter(data.pixels).most_common(top))


def room_boxes(data: MapData) -> dict[int, dict[str, Any]]:
    """Riquadro e centro di ogni stanza, in coordinate del robot."""
    cells: dict[int, list[tuple[int, int]]] = {}
    w = data.width
    for index, value in enumerate(data.pixels):
        room = data.room_of(value)
        if room is not None:
            cells.setdefault(room, []).append((index % w, index // w))
    boxes: dict[int, dict[str, Any]] = {}
    for room, pts in cells.items():
        cols = [p[0] for p in pts]
        rows = [p[1] for p in pts]
        x0 = data.left + min(cols) * data.grid
        x1 = data.left + (max(cols) + 1) * data.grid
        y0 = data.top + min(rows) * data.grid
        y1 = data.top + (max(rows) + 1) * data.grid
        cx = data.left + (sum(cols) / len(cols) + 0.5) * data.grid
        cy = data.top + (sum(rows) / len(rows) + 0.5) * data.grid
        # il centro deve cadere dentro la stanza (stanze a L): prendo la cella più vicina
        best = min(pts, key=lambda p: (p[0] - (cx - data.left) / data.grid) ** 2 + (p[1] - (cy - data.top) / data.grid) ** 2)
        boxes[room] = {
            "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "x": round(data.left + (best[0] + 0.5) * data.grid),
            "y": round(data.top + (best[1] + 0.5) * data.grid),
            "cells": len(pts),
        }
    return boxes


def room_outlines(data: MapData) -> dict[int, list[list[int]]]:
    """Contorno di ogni stanza (poligono che segue i bordi delle celle), in coordinate del robot.

    Per ogni cella della stanza si prendono i lati che confinano con l'esterno,
    orientati in senso orario; concatenandoli si ottengono anelli chiusi e si
    tiene il più lungo (il perimetro esterno). I punti allineati vengono tolti.
    """
    w = data.width
    cells: dict[int, set[tuple[int, int]]] = {}
    for index, value in enumerate(data.pixels):
        room = data.room_of(value)
        if room is not None:
            cells.setdefault(room, set()).add((index % w, index // w))
    result: dict[int, list[list[int]]] = {}
    for room, cs in cells.items():
        edges: dict[tuple[int, int], list[tuple[int, int]]] = {}

        def add(a: tuple[int, int], b: tuple[int, int], edges=edges) -> None:
            edges.setdefault(a, []).append(b)

        for c, r in cs:
            if (c, r - 1) not in cs:
                add((c, r), (c + 1, r))
            if (c + 1, r) not in cs:
                add((c + 1, r), (c + 1, r + 1))
            if (c, r + 1) not in cs:
                add((c + 1, r + 1), (c, r + 1))
            if (c - 1, r) not in cs:
                add((c, r + 1), (c, r))
        best: list[tuple[int, int]] = []
        while edges:
            start = next(iter(edges))
            loop = [start]
            point = start
            while True:
                nxt = edges[point].pop()
                if not edges[point]:
                    del edges[point]
                if nxt == start or nxt not in edges:
                    break
                loop.append(nxt)
                point = nxt
            if len(loop) > len(best):
                best = loop
        # tolgo i punti in mezzo a un tratto rettilineo
        simple: list[tuple[int, int]] = []
        n = len(best)
        for i, p in enumerate(best):
            prev, nxt = best[i - 1], best[(i + 1) % n]
            if (p[0] - prev[0]) * (nxt[1] - p[1]) != (p[1] - prev[1]) * (nxt[0] - p[0]):
                simple.append(p)
        result[room] = [[data.left + c * data.grid, data.top + r * data.grid] for c, r in simple]
    return result


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow vecchio
        return ImageFont.load_default()


def render(
    data: MapData,
    rooms: dict[int, Room],
    highlight: set[int] | None = None,
    zones: list[list[float]] | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Restituisce (PNG, attributi per la card).

    highlight: stanze in pulizia (le altre vengono sbiadite).
    zones: rettangoli in pulizia [x1, y1, x2, y2, ...] in coordinate del robot.
    """
    geo = MapGeometry(data)
    colors = {room_id: ROOM_COLORS[i % len(ROOM_COLORS)] for i, room_id in enumerate(sorted(data.rooms))}
    dim = bool(highlight) or bool(zones)
    # colore per ogni valore di pixel (calcolato una volta sola: i valori diversi sono pochi)
    lookup: dict[int, tuple[int, int, int, int]] = {}
    for value in set(data.pixels):
        if not value:
            continue
        room = data.room_of(value)
        if data.is_wall(value):
            lookup[value] = WALL
        elif room in colors:
            r, g, b = colors[room]
            alpha = 255 if not dim or (highlight and room in highlight) else 90
            lookup[value] = (r, g, b, alpha)
        else:
            lookup[value] = FLOOR
    small = Image.new("RGBA", (data.width, data.height), BACKGROUND)
    px = small.load()
    for index, value in enumerate(data.pixels):
        if value:
            px[index % data.width, data.height - 1 - index // data.width] = lookup[value]
    image = small.resize((geo.width, geo.height), Image.NEAREST)
    draw = ImageDraw.Draw(image)

    # contorno delle stanze in pulizia
    outlines = room_outlines(data)
    for room_id in sorted(highlight or ()):
        points = [geo.to_image(x, y) for x, y in outlines.get(room_id) or []]
        if len(points) > 2:
            draw.line([*points, points[0]], fill=HIGHLIGHT, width=4, joint="curve")

    # zone in pulizia
    if zones:
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        zdraw = ImageDraw.Draw(layer)
        for zone in zones:
            if len(zone) < 4:
                continue
            x1, y1 = geo.to_image(zone[0], zone[1])
            x2, y2 = geo.to_image(zone[2], zone[3])
            box = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
            zdraw.rectangle(box, fill=ZONE_FILL, outline=HIGHLIGHT, width=4)
        image = Image.alpha_composite(image, layer)
        draw = ImageDraw.Draw(image)

    # base di ricarica
    cx, cy = geo.to_image(data.charger[0], data.charger[1])
    draw.ellipse((cx - 7, cy - 7, cx + 7, cy + 7), fill=CHARGER, outline=ROBOT_EDGE, width=2)

    # robot con la direzione
    rx, ry = geo.to_image(data.robot[0], data.robot[1])
    draw.ellipse((rx - 9, ry - 9, rx + 9, ry + 9), fill=ROBOT, outline=ROBOT_EDGE, width=2)
    angle = math.radians(data.robot[2])
    draw.line((rx, ry, rx + 12 * math.cos(angle), ry - 12 * math.sin(angle)), fill=ROBOT_EDGE, width=3)

    # nomi delle stanze
    boxes = room_boxes(data)
    # testo proporzionato all'immagine (in dashboard viene rimpicciolita ~3 volte);
    # un po' sotto il centro, dove la card disegna l'icona della stanza
    size = max(16, round(geo.width / 42))
    font = _font(size)
    for room_id, box in boxes.items():
        room = rooms.get(room_id)
        if not room:
            continue
        tx, ty = geo.to_image(box["x"], box["y"])
        ty = min(ty + size * 1.6, geo.height - size)
        draw.text((tx, ty), room.name, fill=TEXT, font=font, anchor="mm", stroke_width=3, stroke_fill=TEXT_EDGE)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)

    attributes = {
        "calibration_points": geo.calibration_points(),
        "vacuum_position": {"x": data.robot[0], "y": data.robot[1], "a": data.robot[2]},
        "charger_position": {"x": data.charger[0], "y": data.charger[1]},
        "image_size": {"width": geo.width, "height": geo.height},
        "rooms": {
            str(room_id): {
                "name": rooms[room_id].name if room_id in rooms else f"Stanza {room_id}",
                "x0": box["x0"], "y0": box["y0"], "x1": box["x1"], "y1": box["y1"],
                "x": box["x"], "y": box["y"],
                "outline": outlines.get(room_id)
                or [[box["x0"], box["y0"]], [box["x1"], box["y0"]], [box["x1"], box["y1"]], [box["x0"], box["y1"]]],
            }
            for room_id, box in boxes.items()
        },
    }
    return buffer.getvalue(), attributes
