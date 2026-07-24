"""Identidad MGRS compartida por HLSL30 y HLSS30."""

from __future__ import annotations

import re
from collections.abc import Iterable

_HLS_TILE_PATTERN = re.compile(r"^T(?P<tile>[0-9]{2}[A-Z]{3})_")


def mgrs_tile_ids_from_hls_identifiers(identifiers: Iterable[str]) -> tuple[str, ...]:
    """Extrae tiles MGRS ordenadas y falla ante identificadores inesperados."""
    tile_ids: set[str] = set()
    for identifier in identifiers:
        match = _HLS_TILE_PATTERN.match(identifier)
        if match is None:
            raise ValueError("identificador HLS sin tile MGRS reconocible")
        tile_ids.add(match.group("tile"))
    return tuple(sorted(tile_ids))
