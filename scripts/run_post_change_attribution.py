"""Atribuye eventos usando trayectorias RF y evidencia agrícola validada por política."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from deforestation_pipeline.agricultural_evidence import (
    EvaluatedAgriculturalEvidence,
    evaluate_agricultural_evidence,
    load_agricultural_evidence_document,
    load_agricultural_evidence_policy,
)
from deforestation_pipeline.post_change_attribution import (
    AttributionContext,
    materialize_post_change_attribution,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
        help="JSON agricultural-evidence v1; no acepta un booleano independent",
    )
    parser.add_argument(
        "--agricultural-persistence-bundle",
        type=Path,
        help="bundle persistente auditable; excluyente con --agricultural-evidence-json",
    )
    parser.add_argument(
        "--agricultural-evidence-policy",
        type=Path,
        default=PROJECT_ROOT / "configs" / "agricultural-evidence.yml",
        help="política versionada que deriva independencia desde el linaje",
    )
    return parser


def _agricultural_evidence(
    evidence_path: Path | None,
    policy_path: Path,
) -> dict[str, tuple[EvaluatedAgriculturalEvidence, ...]]:
    policy = load_agricultural_evidence_policy(policy_path)
    if evidence_path is None:
        return {}
    document = load_agricultural_evidence_document(evidence_path)
    return evaluate_agricultural_evidence(document, policy)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.agricultural_evidence_json and args.agricultural_persistence_bundle:
            raise ValueError("agricultural evidence JSON y persistence bundle son excluyentes")
        policy = load_agricultural_evidence_policy(args.agricultural_evidence_policy)
        context = AttributionContext(
            declared_land_use=args.declared_land_use,
            declared_context_source=args.declared_context_source,
            agricultural_use_evidence=_agricultural_evidence(
                args.agricultural_evidence_json,
                args.agricultural_evidence_policy,
            ),
        )
        output = materialize_post_change_attribution(
            source_event_bundle=args.event_bundle,
            source_rf_bundle=args.rf_delta_bundle,
            output_root=args.output_root,
            establishment_id=args.establishment_id,
            context=context,
            created_at=datetime.now(UTC),
            source_agricultural_bundle=args.agricultural_persistence_bundle,
            agricultural_policy=policy,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
