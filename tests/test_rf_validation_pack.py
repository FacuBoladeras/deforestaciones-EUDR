from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from PIL import Image

from deforestation_pipeline.rf_validation_pack import (
    _annotate_card,
    _build_sentinel2_yearly_images,
    _download_once,
    _generate_all_cards,
    _generate_card,
    _generate_contact_sheets,
    _mask_sentinel2,
    _predict_rf_rows,
    _sample_valid_counts,
    evaluate_validation_pack,
    load_pack_config,
    prepare_validation_pack,
)


def test_versioned_pack_config_freezes_50_pilot_and_200_test() -> None:
    config = load_pack_config(Path("configs/rf-validation-entrerios.yml"))

    assert config.pilot.quotas_per_utm.total * 2 == 50
    assert config.test.quotas_per_utm.total * 2 == 200
    assert config.gate.minimum_global_forest_recall == 0.90
    assert config.imagery.years == (2019, 2020, 2021)


def test_pack_config_rejects_non_mapping_and_changed_visual_years(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.yml"
    invalid.write_text("- no\n- mapping\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_pack_config(invalid)

    original = Path("configs/rf-validation-entrerios.yml").read_text(encoding="utf-8")
    invalid.write_text(
        original.replace("[2019, 2020, 2021]", "[2018, 2020, 2021]"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="2019"):
        load_pack_config(invalid)


def test_remote_rf_prediction_keeps_raw_vote_fraction_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "deforestation_pipeline.rf_validation_pack.forest_rf_predictor_band_names",
        lambda: ("feature_a",),
    )
    monkeypatch.setattr(
        "deforestation_pipeline.rf_validation_pack.build_geemap_tree_classifier",
        lambda **_kwargs: SimpleNamespace(setOutputMode=lambda _mode: object()),
    )

    class FakeCollection:
        def classify(self, *_args: object) -> FakeCollection:
            return self

        def select(self, _properties: list[str]) -> FakeCollection:
            return self

        def getInfo(self) -> dict[str, object]:
            return {
                "features": [
                    {
                        "properties": {
                            "blind_id": "T-001",
                            "rf_prediction": 1,
                            "raw_votes": [1, 0, 1],
                        }
                    }
                ]
            }

    module = SimpleNamespace(
        Feature=lambda _geometry, properties: properties,
        FeatureCollection=lambda _features: FakeCollection(),
    )
    result = _predict_rf_rows(
        module=module,
        rows=[{"blind_id": "T-001", "feature_a": "0.5"}],
        forest_model_config=SimpleNamespace(expected_tree_count=3),
    )

    assert result == {"T-001": {"rf_prediction": "1", "rf_vote_fraction": "0.666667"}}


def test_sentinel_builder_and_mask_construct_lazy_graphs() -> None:
    module = MagicMock()
    config = load_pack_config(Path("configs/rf-validation-entrerios.yml")).imagery

    images = _build_sentinel2_yearly_images(module=module, config=config)
    masked = _mask_sentinel2(module, MagicMock())

    assert set(images) == {2019, 2020, 2021}
    assert masked is not None
    assert module.ImageCollection.call_count == 3


def test_valid_count_sampling_requires_every_blind_id() -> None:
    sampled = {
        "features": [
            {
                "properties": {
                    "blind_id": "T-001",
                    "valid_observation_count_2019": 12,
                    "valid_observation_count_2020": 18,
                    "valid_observation_count_2021": 16,
                }
            }
        ]
    }
    count_image = MagicMock()
    count_image.select.return_value = count_image
    count_image.addBands.return_value = count_image
    count_image.sampleRegions.return_value.getInfo.return_value = sampled
    module = MagicMock()
    module.FeatureCollection.return_value = MagicMock()

    result = _sample_valid_counts(
        module=module,
        rows=[{"blind_id": "T-001", "longitude": "-60", "latitude": "-31"}],
        annual_images={2019: count_image, 2020: count_image, 2021: count_image},
    )

    assert result["T-001"]["valid_observation_count_2020"] == "18"


def test_card_annotation_and_contact_sheet_do_not_need_coordinates(tmp_path: Path) -> None:
    raw = tmp_path / "raw.png"
    card = tmp_path / "public" / "pilot" / "cards" / "P-001.png"
    Image.new("RGB", (1024, 256), "green").save(raw)
    quality = {
        "valid_observation_count_2019": "10",
        "valid_observation_count_2020": "11",
        "valid_observation_count_2021": "12",
    }

    _annotate_card(raw_path=raw, output_path=card, blind_id="P-001", quality=quality)
    sheets = _generate_contact_sheets([card], public_directory=tmp_path / "public")

    assert card.exists()
    assert Image.open(card).size == (1024, 318)
    assert len(sheets) == 1
    assert sheets[0].exists()


def test_generate_card_uses_one_download_and_removes_raw(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_thumbnail = tmp_path / "source.png"
    Image.new("RGB", (1024, 256), "blue").save(raw_thumbnail)

    def fake_download(_url: str, target: Path) -> None:
        target.write_bytes(raw_thumbnail.read_bytes())

    monkeypatch.setattr(
        "deforestation_pipeline.rf_validation_pack._download_once",
        fake_download,
    )
    module = MagicMock()
    module.ImageCollection.fromImages.return_value.mosaic.return_value.getThumbURL.return_value = (
        "https://example.invalid/thumbnail"
    )
    annual = {year: MagicMock() for year in (2019, 2020, 2021)}
    output = tmp_path / "T-001.png"

    result = _generate_card(
        module=module,
        row={
            "blind_id": "T-001",
            "longitude": "-60.1",
            "latitude": "-31.2",
        },
        quality={
            "valid_observation_count_2019": "1",
            "valid_observation_count_2020": "2",
            "valid_observation_count_2021": "3",
        },
        annual_images=annual,
        imagery=load_pack_config(Path("configs/rf-validation-entrerios.yml")).imagery,
        output_path=output,
    )

    assert result == output
    assert output.exists()
    assert not output.with_suffix(".raw.png").exists()


def test_parallel_card_generation_records_failure_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_generate_card(**kwargs: Any) -> Path:
        row = kwargs["row"]
        calls.append(row["blind_id"])
        if row["blind_id"] == "T-002":
            raise OSError("remote failed")
        output: Path = kwargs["output_path"]
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (20, 20), "green").save(output)
        return output

    monkeypatch.setattr(
        "deforestation_pipeline.rf_validation_pack._generate_card",
        fake_generate_card,
    )
    rows = [
        {"blind_id": "T-001", "phase": "test"},
        {"blind_id": "T-002", "phase": "test"},
    ]
    quality = {
        blind_id: {
            "valid_observation_count_2019": "1",
            "valid_observation_count_2020": "1",
            "valid_observation_count_2021": "1",
        }
        for blind_id in ("T-001", "T-002")
    }

    cards, status = _generate_all_cards(
        module=object(),
        rows=rows,
        quality=quality,
        annual_images={},
        imagery=load_pack_config(Path("configs/rf-validation-entrerios.yml")).imagery,
        public_directory=tmp_path,
    )

    assert sorted(calls) == ["T-001", "T-002"]
    assert status == {"T-001": "generated", "T-002": "failed"}
    assert len(cards) == 2
    assert all(path.exists() for path in cards)


def test_download_once_rejects_tiny_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"tiny"

    monkeypatch.setattr(
        "deforestation_pipeline.rf_validation_pack.urllib.request.urlopen",
        lambda *_args, **_kwargs: Response(),
    )
    target = tmp_path / "tiny.png"
    with pytest.raises(OSError, match="demasiado pequeño"):
        _download_once("https://example.invalid", target)
    assert not target.exists()


def test_prepare_pack_orchestrates_public_private_split(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_pack_config(Path("configs/rf-validation-entrerios.yml")).model_copy(
        update={"output_directory": tmp_path / "outputs"}
    )
    monkeypatch.setattr(
        "deforestation_pipeline.rf_validation_pack.load_pack_config",
        lambda _path: config,
    )
    monkeypatch.setattr(
        "deforestation_pipeline.rf_validation_pack.read_csv_rows",
        lambda _paths: [{"placeholder": "1"}],
    )

    def fake_select(_rows: object, *, split: str, **_kwargs: object) -> list[dict[str, str]]:
        size = 50 if split == "validation" else 200
        prefix = "pilot" if split == "validation" else "test"
        return [
            {
                "sample_id": f"{prefix}-{index}",
                "block_id": f"b-{index}",
                "split": split,
                "proxy_label": ("forest" if index % 2 else "non_forest"),
                "longitude": "-60",
                "latitude": "-31",
                "utm_zone": "20" if index % 2 else "21",
            }
            for index in range(size)
        ]

    monkeypatch.setattr(
        "deforestation_pipeline.rf_validation_pack.select_stratified_rows",
        fake_select,
    )
    monkeypatch.setattr(
        "deforestation_pipeline.rf_validation_pack.authenticate_earth_engine_user_oauth",
        lambda **_kwargs: SimpleNamespace(module=object()),
    )
    monkeypatch.setattr(
        "deforestation_pipeline.rf_validation_pack.load_config",
        lambda _path: SimpleNamespace(forest_model=object()),
    )
    monkeypatch.setattr(
        "deforestation_pipeline.rf_validation_pack._predict_rf_rows",
        lambda **kwargs: {
            row["blind_id"]: {"rf_prediction": "1", "rf_vote_fraction": "0.8"}
            for row in kwargs["rows"]
        },
    )
    monkeypatch.setattr(
        "deforestation_pipeline.rf_validation_pack._build_sentinel2_yearly_images",
        lambda **_kwargs: {},
    )
    monkeypatch.setattr(
        "deforestation_pipeline.rf_validation_pack._sample_valid_counts",
        lambda **kwargs: {
            row["blind_id"]: {
                "valid_observation_count_2019": "10",
                "valid_observation_count_2020": "10",
                "valid_observation_count_2021": "10",
            }
            for row in kwargs["rows"]
        },
    )

    def fake_cards(**kwargs: Any) -> tuple[list[Path], dict[str, str]]:
        paths: list[Path] = []
        statuses: dict[str, str] = {}
        for row in kwargs["rows"]:
            path = kwargs["public_directory"] / row["phase"] / "cards" / f"{row['blind_id']}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"card")
            paths.append(path)
            statuses[row["blind_id"]] = "generated"
        return paths, statuses

    monkeypatch.setattr(
        "deforestation_pipeline.rf_validation_pack._generate_all_cards",
        fake_cards,
    )

    def fake_sheets(_cards: object, *, public_directory: Path) -> list[Path]:
        path = public_directory / "test" / "contact_sheets" / "page-01.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"sheet")
        return [path]

    monkeypatch.setattr(
        "deforestation_pipeline.rf_validation_pack._generate_contact_sheets",
        fake_sheets,
    )
    result = prepare_validation_pack(
        Path("configs/rf-validation-entrerios.yml"),
        generated_at=datetime(2026, 8, 3, tzinfo=UTC),
    )
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert result.card_count == 250
    assert result.pilot_count == 50
    assert result.test_count == 200
    assert result.private_table_path.exists()
    assert manifest["selection_uses_rf_prediction"] is False
    assert manifest["quality_summary"]["failed_cards"] == 0


def test_evaluate_pack_joins_completed_labels_and_writes_metrics(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    private = pack / "private_sealed_do_not_open_during_review"
    public = pack / "public_blind_review" / "test"
    private.mkdir(parents=True)
    public.mkdir(parents=True)
    (pack / "pack_manifest.json").write_text(
        json.dumps(
            {
                "gate": {
                    "minimum_adjudicable_fraction": 0.80,
                    "minimum_global_forest_recall": 0.90,
                    "minimum_utm_forest_recall": 0.85,
                    "minimum_forest_precision": 0.80,
                    "minimum_balanced_accuracy": 0.85,
                    "minimum_open_woody_recall": 0.70,
                }
            }
        ),
        encoding="utf-8",
    )
    (private / "sealed_sample.csv").write_text(
        "blind_id,phase,utm_zone,rf_prediction,rf_vote_fraction\n"
        "T-001,test,20,1,0.9\n"
        "T-002,test,21,0,0.1\n",
        encoding="utf-8",
    )
    adjudications = public / "completed.csv"
    adjudications.write_text(
        "blind_id,reference_label,confidence,reason_code,reviewer_id,reviewed_at,notes\n"
        "T-001,forest,high,open_woody,r1,2026-08-03,\n"
        "T-002,non_forest,high,pasture,r1,2026-08-03,\n",
        encoding="utf-8",
    )

    result = evaluate_validation_pack(pack, adjudication_path=adjudications)

    assert result.report_path.exists()
    assert result.confusion_matrix_path.exists()
    assert result.passed is False
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["global_metrics"]["true_positive"] == 1
