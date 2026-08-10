"""Atribuye eventos existentes usando sus trayectorias RF 2020-2024."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from deforestation_pipeline.post_change_attribution import (
    AgriculturalLandUse,
    AgriculturalUseEvidence,
    AttributionContext,
    materialize_post_change_attribution,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event_bundle", type=Path)
    parser.add_argument("rf_delta_bundle", type=Path)
    parser.add_argument("--output-root", type=Path, default=Path("outputs/attribution"))
    parser.add_argument("--establishment-id", required=True)
    parser.add_argument(
        "--declared-land-use",
        choices=("unknown", "managed_forest_plantation"),
        default="unknown",
    )
    parser.add_argument("--declared-context-source", default="not_provided")
    parser.add_argument(
        "--agricultural-evidence-json",
        type=Path,
        help="JSON opcional con events[{event_id,land_use,source,independent}]",
    )
    return parser


def _agricultural_evidence(path: Path | None) -> dict[str, AgriculturalUseEvidence]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("agricultural evidence JSON requiere events[]")
    evidence: dict[str, AgriculturalUseEvidence] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("event_id"), str):
            raise ValueError("agricultural evidence contiene una fila inválida")
        event_id = row["event_id"]
        if event_id in evidence:
            raise ValueError(f"agricultural evidence repite event_id:{event_id}")
        land_use = row.get("land_use")
        independent = row.get("independent")
        source = row.get("source")
        if land_use not in {"crop", "pasture", "livestock_infrastructure"}:
            raise ValueError(f"agricultural evidence land_use inválido:{event_id}")
        if not isinstance(independent, bool):
            raise ValueError(f"agricultural evidence independent debe ser boolean:{event_id}")
        if not isinstance(source, str):
            raise ValueError(f"agricultural evidence source debe ser string:{event_id}")
        evidence[event_id] = AgriculturalUseEvidence(
            land_use=cast(AgriculturalLandUse, land_use),
            source=source,
            independent=independent,
        )
    return evidence


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        context = AttributionContext(
            declared_land_use=args.declared_land_use,
            declared_context_source=args.declared_context_source,
            agricultural_use_evidence=_agricultural_evidence(args.agricultural_evidence_json),
        )
        output = materialize_post_change_attribution(
            source_event_bundle=args.event_bundle,
            source_rf_bundle=args.rf_delta_bundle,
            output_root=args.output_root,
            establishment_id=args.establishment_id,
            context=context,
            created_at=datetime.now(UTC),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
