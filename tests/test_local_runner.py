"""Pruebas del corte vertical local y sus artefactos auditables."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from deforestation_pipeline.local_runner import (
    LocalVectorInputError,
    main,
    run_local_vector_pipeline,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "default.yml"
FIXED_NOW = datetime(2026, 7, 23, 18, 30, tzinfo=UTC)


def _write_feature(path: Path) -> bytes:
    payload = {
        "type": "Feature",
        "properties": {"name": "caso sintético"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [-63.0, -33.0],
                    [-62.99, -33.0],
                    [-62.99, -32.99],
                    [-63.0, -32.99],
                    [-63.0, -33.0],
                ]
            ],
        },
    }
    content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
    path.write_bytes(content)
    return content


def test_local_run_writes_complete_auditable_bundle(tmp_path: Path) -> None:
    input_path = tmp_path / "establecimiento.geojson"
    original_bytes = _write_feature(input_path)

    run_directory = run_local_vector_pipeline(
        input_path=input_path,
        output_root=tmp_path / "outputs",
        config_path=DEFAULT_CONFIG,
        source_crs="EPSG:4326",
        establishment_id="test-establishment",
        analysis_end_date=date(2026, 7, 23),
        created_at=FIXED_NOW,
    )

    expected_files = {
        "config/resolved_config.json",
        "environment.json",
        "geometry/analysis_geometry.geojson",
        "geometry/normalized_wgs84.geojson",
        "geometry/original_interpreted.json",
        "geometry/validation.json",
        "input/source.geojson",
        "manifest.json",
        "measurements/area.json",
        "run_summary.json",
    }
    actual_files = {
        path.relative_to(run_directory).as_posix()
        for path in run_directory.rglob("*")
        if path.is_file()
    }
    assert actual_files == expected_files
    assert (run_directory / "input/source.geojson").read_bytes() == original_bytes

    summary = json.loads((run_directory / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["establishment_id"] == "test-establishment"
    assert summary["stage"] == "spatial_preparation"
    assert summary["final_assessment_generated"] is False
    assert summary["area"]["total_area_ha"] > 0
    assert summary["area"]["calculation_crs"] == "EPSG:6933"
    assert summary["limitations"]

    manifest = json.loads((run_directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_self_excluded"] is True
    assert manifest["remote_data_accessed"] is False
    assert manifest["datasets"] == []
    manifested_paths = {artifact["path"] for artifact in manifest["artifacts"]}
    assert manifested_paths == expected_files - {"manifest.json"}
    for artifact in manifest["artifacts"]:
        artifact_path = run_directory / artifact["path"]
        assert artifact["sha256"] == hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        assert artifact["size_bytes"] == artifact_path.stat().st_size


def test_feature_collection_must_contain_exactly_one_feature(tmp_path: Path) -> None:
    input_path = tmp_path / "multiple.geojson"
    input_path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {"type": "Point", "coordinates": [-63, -33]},
                    },
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {"type": "Point", "coordinates": [-62, -32]},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(LocalVectorInputError, match="exactamente una Feature"):
        run_local_vector_pipeline(
            input_path=input_path,
            output_root=tmp_path / "outputs",
            config_path=DEFAULT_CONFIG,
            source_crs="EPSG:4326",
            establishment_id="multiple",
            analysis_end_date=date(2026, 7, 23),
            created_at=FIXED_NOW,
        )

    assert not (tmp_path / "outputs").exists()


def test_existing_run_directory_is_never_overwritten(tmp_path: Path) -> None:
    input_path = tmp_path / "establecimiento.geojson"
    _write_feature(input_path)
    run_local_vector_pipeline(
        input_path=input_path,
        output_root=tmp_path / "outputs",
        config_path=DEFAULT_CONFIG,
        source_crs="EPSG:4326",
        establishment_id="test-establishment",
        analysis_end_date=date(2026, 7, 23),
        created_at=FIXED_NOW,
    )

    with pytest.raises(FileExistsError, match="no se sobrescribe"):
        run_local_vector_pipeline(
            input_path=input_path,
            output_root=tmp_path / "outputs",
            config_path=DEFAULT_CONFIG,
            source_crs="EPSG:4326",
            establishment_id="test-establishment",
            analysis_end_date=date(2026, 7, 23),
            created_at=FIXED_NOW,
        )


def test_cli_runs_sample_and_prints_created_directory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = main(
        [
            str(PROJECT_ROOT / "data" / "samples" / "local_test_polygon.geojson"),
            "--output-root",
            str(tmp_path),
            "--establishment-id",
            "cli-test",
            "--analysis-end-date",
            "2026-07-23",
        ]
    )

    run_directory = Path(capsys.readouterr().out.strip())
    assert result == 0
    assert run_directory.parent == tmp_path.resolve()
    assert (run_directory / "manifest.json").is_file()
