"""Primitivas determinísticas compartidas para segmentar dominios candidatos."""

from __future__ import annotations

import hashlib
import math
from collections import deque

import numpy as np
from numpy.typing import NDArray

from deforestation_pipeline.schemas import RasterGridSpec


def connected_components_8(mask: NDArray[np.bool_]) -> list[tuple[tuple[int, int], ...]]:
    """Devuelve componentes de ocho vecinos sin rellenar huecos."""
    height, width = mask.shape
    visited = np.zeros_like(mask, dtype=np.bool_)
    components: list[tuple[tuple[int, int], ...]] = []
    offsets = (
        (-1, -1),
        (-1, 0),
        (-1, 1),
        (0, -1),
        (0, 1),
        (1, -1),
        (1, 0),
        (1, 1),
    )
    for row in range(height):
        for column in range(width):
            if not mask[row, column] or visited[row, column]:
                continue
            queue: deque[tuple[int, int]] = deque(((row, column),))
            visited[row, column] = True
            pixels: list[tuple[int, int]] = []
            while queue:
                current_row, current_column = queue.popleft()
                pixels.append((current_row, current_column))
                for row_offset, column_offset in offsets:
                    candidate_row = current_row + row_offset
                    candidate_column = current_column + column_offset
                    if not (0 <= candidate_row < height and 0 <= candidate_column < width):
                        continue
                    if (
                        mask[candidate_row, candidate_column]
                        and not visited[candidate_row, candidate_column]
                    ):
                        visited[candidate_row, candidate_column] = True
                        queue.append((candidate_row, candidate_column))
            components.append(tuple(sorted(pixels)))
    return components


def spatial_candidate_id(
    *,
    prefix: str,
    grid_sha256: str,
    pixels: tuple[tuple[int, int], ...],
) -> str:
    """Construye una identidad estable dependiente sólo de grilla y footprint."""
    pixel_identity = ";".join(f"{row},{column}" for row, column in pixels)
    digest = hashlib.sha256(f"{grid_sha256}|{pixel_identity}".encode()).hexdigest()[:12].upper()
    return f"{prefix}-{digest}"


def grid_pixel_area_ha(grid_spec: RasterGridSpec, *, error_code: str) -> float:
    """Calcula área afín por píxel en hectáreas sin asumir eje sin rotación."""
    x_scale, x_shear, _, y_shear, y_scale, _ = grid_spec.transform
    area = abs(x_scale * y_scale - x_shear * y_shear) / 10_000.0
    if not math.isfinite(area) or area <= 0:
        raise ValueError(error_code)
    return area
