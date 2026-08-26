"""Carga verificable del contrato editorial ya publicado."""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import ValidationError

from deforestation_reporting.models import (
    AnalysisOverview,
    AnnualForestPoint,
    BaselineSummary,
    ClientFigure,
    ComponentSummary,
    DatasetSummary,
    EventSummary,
    EvidenceGate,
    GeometryRings,
    GeometrySummary,
    HeadlineMetrics,
    QualitySummary,
    ReportViewModel,
)

GateKey = Literal[
    "forest_at_cutoff",
    "post_cutoff_loss",
    "persistent_change",
    "agricultural_or_livestock_post_use",
    "defensible_area_and_geometry",
    "strong_alternative_explanation_absent",
    "visec_operational_area_strictly_greater_than_threshold",
]
MainClientFigureKind = Literal[
    "annual_forest_change",
    "rgb_timeline",
    "spectral_index_timeline",
    "observation_coverage",
    "disturbance_detection",
    "post_change_attribution",
]

_GATE_KEYS: tuple[GateKey, ...] = (
    "forest_at_cutoff",
    "post_cutoff_loss",
    "persistent_change",
    "agricultural_or_livestock_post_use",
    "defensible_area_and_geometry",
    "strong_alternative_explanation_absent",
)

_OPERATIONAL_AREA_GATE: GateKey = "visec_operational_area_strictly_greater_than_threshold"

_SUPERSEDED_DETECTION_LIMITATIONS_AFTER_ATTRIBUTION = {
    "Se detectaron señales de perturbación sin atribuir su causa o uso posterior.",
    "No se ejecutó atribución de conversión ni se generó una conclusión final.",
}


class ReportIntegrityError(RuntimeError):
    """Un archivo publicado no coincide con su contrato de integridad."""


class ReportContractError(ValueError):
    """El dataset no contiene los campos editoriales mínimos."""


@dataclass(frozen=True, slots=True)
class VerifiedReportAsset:
    report_path: str
    absolute_path: Path
    category: str
    component: str | None
    event_id: str | None
    selection_reason: str | None
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ReportPackage:
    root: Path
    dataset: dict[str, Any]
    assets: tuple[VerifiedReportAsset, ...]
    establishment_geometry: GeometryRings


def load_report_package(
    result_root: Path,
    *,
    input_path: Path | None = None,
) -> ReportPackage:
    """Verifica dataset y allowlist completa antes de exponer paths al renderer."""
    root = result_root.resolve()
    index_path = _safe_file(root, "report_assets/index.json", "report_index")
    index = _load_object(index_path, "report_index_invalid")
    dataset_relative = _required_string(index.get("dataset_path"), "dataset_path_invalid")
    dataset_digest = _required_sha256(index.get("dataset_sha256"), "dataset_sha256_invalid")
    dataset_path = _safe_file(root, dataset_relative, "dataset")
    if _sha256(dataset_path) != dataset_digest:
        raise ReportIntegrityError("dataset_sha256_mismatch")
    dataset = _load_object(dataset_path, "report_dataset_invalid")

    entries = index.get("files")
    if not isinstance(entries, list):
        raise ReportIntegrityError("asset_allowlist_invalid")
    assets: list[VerifiedReportAsset] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ReportIntegrityError("asset_entry_invalid")
        report_path = _required_string(entry.get("report_path"), "asset_path_invalid")
        if report_path in seen:
            raise ReportIntegrityError("asset_path_duplicate")
        seen.add(report_path)
        absolute_path = _safe_file(root, report_path, "asset")
        expected_size = entry.get("size_bytes")
        if not isinstance(expected_size, int) or expected_size <= 0:
            raise ReportIntegrityError("asset_size_invalid")
        if absolute_path.stat().st_size != expected_size:
            raise ReportIntegrityError("asset_size_mismatch")
        expected_digest = _required_sha256(entry.get("report_sha256"), "asset_sha256_invalid")
        if _sha256(absolute_path) != expected_digest:
            raise ReportIntegrityError("asset_sha256_mismatch")
        assets.append(
            VerifiedReportAsset(
                report_path=report_path,
                absolute_path=absolute_path,
                category=_required_string(entry.get("category"), "asset_category_invalid"),
                component=_optional_string(entry.get("component"), "asset_component_invalid"),
                event_id=_optional_string(entry.get("event_id"), "asset_event_id_invalid"),
                selection_reason=_optional_string(
                    entry.get("selection_reason"), "asset_selection_reason_invalid"
                ),
                sha256=expected_digest,
                size_bytes=expected_size,
            )
        )
    assets.sort(key=lambda item: item.report_path)
    return ReportPackage(
        root=root,
        dataset=dataset,
        assets=tuple(assets),
        establishment_geometry=_load_declared_geometry(root, input_path=input_path),
    )


def _load_declared_geometry(root: Path, *, input_path: Path | None = None) -> GeometryRings:
    """Carga el perímetro declarado sólo cuando su hash publicado es verificable."""
    manifest_path = root / "run_manifest.json"
    if not manifest_path.is_file():
        return ()
    manifest = _load_object(manifest_path, "run_manifest_invalid")
    input_entry = manifest.get("input")
    if not isinstance(input_entry, dict):
        return ()
    resolved_input = (input_path or root.parent / "input.geojson").resolve()
    if not resolved_input.is_file():
        raise ReportIntegrityError("input_geometry_missing")
    expected_size = input_entry.get("size_bytes")
    if not isinstance(expected_size, int) or expected_size <= 0:
        raise ReportIntegrityError("input_geometry_size_invalid")
    if resolved_input.stat().st_size != expected_size:
        raise ReportIntegrityError("input_geometry_size_mismatch")
    expected_digest = _required_sha256(input_entry.get("sha256"), "input_geometry_sha256_invalid")
    if _sha256(resolved_input) != expected_digest:
        raise ReportIntegrityError("input_geometry_sha256_mismatch")
    payload = _load_object(resolved_input, "input_geometry_invalid")
    try:
        if payload.get("type") == "Feature":
            geometry = _mapping(payload.get("geometry"), "input_geometry_invalid")
            return _geometry_rings(geometry)
        if payload.get("type") == "FeatureCollection":
            features = payload.get("features")
            if not isinstance(features, list):
                raise ReportContractError("input_geometry_invalid")
            rings: list[tuple[tuple[float, float], ...]] = []
            for feature in features:
                feature_map = _mapping(feature, "input_geometry_invalid")
                geometry = _mapping(feature_map.get("geometry"), "input_geometry_invalid")
                rings.extend(_geometry_rings(geometry))
            return tuple(rings)
        return _geometry_rings(payload)
    except ReportContractError as error:
        raise ReportIntegrityError("input_geometry_invalid") from error


def build_report_view_model(package: ReportPackage) -> ReportViewModel:
    """Construye la vista cliente desde datos verificados, nunca desde figuras internas."""
    dataset = package.dataset
    try:
        analysis_source = _mapping(dataset.get("analysis"), "analysis_missing")
        analysis = AnalysisOverview.model_validate(
            {
                "analysis_id": analysis_source.get("analysis_id"),
                "establishment_id": analysis_source.get("establishment_id"),
                "analysis_end_date": analysis_source.get("analysis_end_date"),
                "overall_status": analysis_source.get("overall_status"),
                "recorded_at": dataset.get("recorded_at"),
            }
        )
        disturbance_summary = _verified_object(
            package, "report_assets/data/main/030_disturbance_events.json"
        )
        metrics_source = _mapping(dataset.get("headline_metrics"), "headline_metrics_missing")
        candidate_area, candidate_count, operational_area, operational_count = (
            _headline_event_metrics(metrics_source, disturbance_summary)
        )
        metrics = HeadlineMetrics.model_validate(
            {
                "establishment_area_ha": metrics_source.get("establishment_area_ha"),
                "forest_area_2020_ha": metrics_source.get("forest_area_2020_ha"),
                "spectral_candidate_area_ha": candidate_area,
                "spectral_candidate_count": candidate_count,
                "candidate_episode_area_ha": operational_area,
                "candidate_episode_count": operational_count,
                "likely_conversion_area_ha": metrics_source.get("likely_conversion_area_ha"),
                "conversion_likely_count": metrics_source.get("conversion_likely_count"),
                "automatic_final_assessment_generated": metrics_source.get(
                    "automatic_final_assessment_generated", False
                ),
            }
        )
        components = _components(dataset.get("component_statuses"))
        full_summary = _verified_object(
            package, "report_assets/data/main/010_full_pipeline_summary.json"
        )
        forest_summary = _verified_object(
            package, "report_assets/data/main/020_forest_baseline_2020.json"
        )
        persistence_summary = _verified_component_object(
            package,
            components,
            "agricultural_persistence",
            "report_assets/data/main/070_agricultural_persistence.json",
        )
        attribution_summary = _verified_component_object(
            package,
            components,
            "post_change_attribution",
            "report_assets/data/main/080_post_change_attribution.json",
        )
        agricultural_metadata = _verified_component_object(
            package,
            components,
            "agricultural_collection",
            "report_assets/annex/methodology/agricultural_evidence_metadata.json",
        )
        disturbance_spatial = _verified_object(
            package, "report_assets/annex/spatial/disturbance_events.geojson"
        )
        attribution_spatial = _verified_component_object(
            package,
            components,
            "post_change_attribution",
            "report_assets/annex/spatial/post_change_attribution.geojson",
        )
        seasonal_rows = _verified_csv(package, "report_assets/annex/tables/seasonal_summary.csv")
        detection_rows = _verified_csv(
            package, "report_assets/annex/tables/disturbance_period_summary.csv"
        )
        geometry = _geometry(full_summary)
        baseline = _baseline(metrics, forest_summary)
        quality = _quality(seasonal_rows, detection_rows)
        datasets = _datasets(full_summary, agricultural_metadata)
        events = _events(
            dataset,
            disturbance_summary,
            persistence_summary,
            attribution_summary,
            disturbance_spatial,
            attribution_spatial,
        )
        client_figures = _client_figures(package, events)
        limitations = _limitations(dataset.get("limitations"), components)
        if not limitations:
            raise ReportContractError("limitations_missing")
        return ReportViewModel(
            analysis=analysis,
            verified_asset_count=len(package.assets),
            result_status=(
                _contract_required_string(
                    attribution_summary.get("status"), "result_status_invalid"
                )
                if attribution_summary
                else "partial_incomplete_evidence"
            ),
            metrics=metrics,
            geometry=geometry,
            establishment_geometry=package.establishment_geometry,
            seasonal_period_ids=_seasonal_period_ids(full_summary, seasonal_rows),
            baseline=baseline,
            quality=quality,
            datasets=datasets,
            client_figures=client_figures,
            components=components,
            events=events,
            limitations=limitations,
        )
    except (ValidationError, TypeError, ValueError) as error:
        if isinstance(error, ReportContractError):
            raise
        raise ReportContractError("report_dataset_invalid") from error


def _headline_event_metrics(
    metrics: dict[str, Any],
    disturbance_summary: dict[str, Any],
) -> tuple[Any, Any, Any, Any]:
    """Separa métricas candidatas y operativas, incluso para datasets editoriales 1.0."""
    if "spectral_candidate_area_ha" in metrics and "spectral_candidate_count" in metrics:
        return (
            metrics.get("spectral_candidate_area_ha"),
            metrics.get("spectral_candidate_count"),
            metrics.get("candidate_episode_area_ha"),
            metrics.get("candidate_episode_count"),
        )
    if "candidate_event_area_ha" in metrics and "candidate_event_count" in metrics:
        return (
            metrics.get("candidate_event_area_ha"),
            metrics.get("candidate_event_count"),
            metrics.get("detected_event_area_ha"),
            metrics.get("detected_event_count"),
        )

    candidate_area = metrics.get("detected_event_area_ha")
    candidate_count = metrics.get("detected_event_count")
    events = disturbance_summary.get("events")
    preserved_events = events if isinstance(events, list) else []
    operational_events = [
        event
        for event in preserved_events
        if isinstance(event, dict)
        and (
            event.get("candidate_footprint_above_visec_area_reference") is True
            or (
                disturbance_summary.get("schema_version") != "1.2.0"
                and event.get("area_threshold_met") is True
            )
        )
    ]
    operational_count = disturbance_summary.get(
        "above_visec_area_reference_candidate_count",
        disturbance_summary.get(
            "operational_event_count",
            disturbance_summary.get("area_threshold_event_count", len(operational_events)),
        ),
    )
    operational_area = disturbance_summary.get("above_visec_area_reference_candidate_area_ha")
    if operational_area is None:
        operational_area = disturbance_summary.get("operational_event_area_ha")
    if operational_area is None:
        operational_area = sum(
            float(event["area_ha"])
            for event in operational_events
            if isinstance(event.get("area_ha"), int | float)
        )
    return candidate_area, candidate_count, operational_area, operational_count


def _client_figures(
    package: ReportPackage, events: tuple[EventSummary, ...]
) -> tuple[ClientFigure, ...]:
    assets_by_path = {asset.report_path: asset for asset in package.assets}
    selected: list[ClientFigure] = []
    main_figure_specs: tuple[tuple[MainClientFigureKind, str], ...] = (
        (
            "annual_forest_change",
            "report_assets/figures/main/200_rf_forest_deltas_timeline.png",
        ),
        ("rgb_timeline", "report_assets/figures/main/030_rgb_timeline.png"),
        (
            "spectral_index_timeline",
            "report_assets/figures/main/040_spectral_index_timeline.png",
        ),
        (
            "observation_coverage",
            "report_assets/figures/main/050_observation_coverage.png",
        ),
        (
            "disturbance_detection",
            "report_assets/figures/main/060_disturbance_detection.png",
        ),
        (
            "post_change_attribution",
            "report_assets/figures/main/500_post_change_attribution.png",
        ),
    )
    for kind, report_path in main_figure_specs:
        asset = assets_by_path.get(report_path)
        if asset is not None:
            selected.append(
                ClientFigure(
                    kind=kind,
                    report_path=asset.report_path,
                    absolute_path=asset.absolute_path,
                    sha256=asset.sha256,
                )
            )
    for event in events:
        if not event.selected_for_detail:
            continue
        report_path = f"report_assets/figures/events/{event.candidate_id}/disturbance.png"
        asset = assets_by_path.get(report_path)
        if asset is not None:
            selected.append(
                ClientFigure(
                    kind="event_spectral_evidence",
                    event_id=event.candidate_id,
                    report_path=asset.report_path,
                    absolute_path=asset.absolute_path,
                    sha256=asset.sha256,
                )
            )
    return tuple(selected)


def _components(value: object) -> tuple[ComponentSummary, ...]:
    source = _mapping(value, "component_statuses_missing")
    result: list[ComponentSummary] = []
    for name, payload in sorted(source.items()):
        item = _mapping(payload, "component_status_invalid")
        status = _contract_required_string(item.get("status"), "component_status_invalid")
        result.append(
            ComponentSummary(
                name=name,
                status=status,
                available=item.get("available", status == "completed"),
                reason=_optional_string(item.get("reason"), "component_reason_invalid"),
                selected_figure_count=item.get("selected_figure_count", 0),
            )
        )
    if not result:
        raise ReportContractError("component_statuses_missing")
    return tuple(result)


def _verified_component_object(
    package: ReportPackage,
    components: tuple[ComponentSummary, ...],
    component_name: str,
    report_path: str,
) -> dict[str, Any]:
    component = next(
        (candidate for candidate in components if candidate.name == component_name), None
    )
    if component is None:
        return _verified_object(package, report_path)
    if not component.available:
        return {}
    return _verified_object(package, report_path)


def _limitations(value: object, components: tuple[ComponentSummary, ...]) -> tuple[str, ...]:
    limitations = _string_list(value)
    attribution_completed = any(
        component.name == "post_change_attribution" and component.status == "completed"
        for component in components
    )
    if attribution_completed:
        limitations = [
            item
            for item in limitations
            if item not in _SUPERSEDED_DETECTION_LIMITATIONS_AFTER_ATTRIBUTION
        ]
    return tuple(limitations)


def _geometry(full_summary: dict[str, Any]) -> GeometrySummary:
    geometry = _mapping(full_summary.get("geometry"), "geometry_summary_missing")
    area = _mapping(full_summary.get("area"), "area_summary_missing")
    return GeometrySummary(
        geometry_type=_contract_required_string(
            geometry.get("analysis_geometry_type"), "geometry_type_invalid"
        ),
        normalized_crs=_contract_required_string(
            geometry.get("normalized_crs"), "normalized_crs_invalid"
        ),
        repair_performed=geometry.get("repair_performed") is True,
        calculation_crs=_contract_required_string(
            area.get("calculation_crs"), "calculation_crs_invalid"
        ),
        area_method=_contract_required_string(area.get("method"), "area_method_invalid"),
        threshold_ha=_required_float(area.get("threshold_ha"), "threshold_invalid"),
    )


def _baseline(metrics: HeadlineMetrics, forest_summary: dict[str, Any]) -> BaselineSummary:
    screening = _mapping(forest_summary.get("screening"), "forest_screening_missing")
    values = _mapping(screening.get("metrics"), "forest_screening_metrics_missing")
    return BaselineSummary(
        vector_establishment_area_ha=_required_float(
            metrics.establishment_area_ha, "establishment_area_invalid"
        ),
        raster_aoi_area_ha=_required_float(values.get("aoi_raster_area_ha"), "raster_area_invalid"),
        automated_evaluable_area_ha=_required_float(
            values.get("automated_evaluable_area_ha"), "evaluable_area_invalid"
        ),
        automated_forest_area_ha=_required_float(
            values.get("automated_forest_area_ha"), "forest_area_invalid"
        ),
        automated_nonforest_area_ha=_required_float(
            values.get("automated_nonforest_area_ha"), "nonforest_area_invalid"
        ),
        review_required_area_ha=_required_float(
            values.get("review_required_area_ha"), "review_area_invalid"
        ),
        insufficient_data_area_ha=_required_float(
            values.get("insufficient_data_area_ha"), "insufficient_area_invalid"
        ),
    )


def _quality(
    seasonal_rows: list[dict[str, str]], detection_rows: list[dict[str, str]]
) -> QualitySummary:
    seasonal_periods = {
        row["period_id"] for row in seasonal_rows if row.get("period_id", "").strip()
    }
    valid_fractions = [
        _csv_float(row.get("valid_pixel_fraction"), "valid_pixel_fraction_invalid")
        for row in seasonal_rows
    ]
    observation_counts = [
        _csv_float(row.get("mean_valid_observation_count"), "observation_count_invalid")
        for row in detection_rows
    ]
    evaluable_count = sum(
        not _csv_bool(row.get("cutoff_straddling_excluded")) for row in detection_rows
    )
    return QualitySummary(
        seasonal_period_count=len(seasonal_periods),
        minimum_valid_pixel_fraction=min(valid_fractions) if valid_fractions else None,
        detection_period_count=len(detection_rows),
        evaluable_detection_period_count=evaluable_count,
        minimum_mean_observation_count=min(observation_counts) if observation_counts else None,
        maximum_mean_observation_count=max(observation_counts) if observation_counts else None,
    )


def _seasonal_period_ids(
    full_summary: dict[str, Any], seasonal_rows: list[dict[str, str]]
) -> tuple[str, ...]:
    gee = _optional_mapping(full_summary.get("gee"))
    raw_periods = gee.get("period_ids")
    if isinstance(raw_periods, list):
        periods = tuple(
            item.strip() for item in raw_periods if isinstance(item, str) and item.strip()
        )
        if periods:
            return periods
    ordered: list[str] = []
    for row in seasonal_rows:
        period_id = row.get("period_id", "").strip()
        if period_id and period_id not in ordered:
            ordered.append(period_id)
    return tuple(ordered)


def _datasets(
    full_summary: dict[str, Any], agricultural_metadata: dict[str, Any]
) -> tuple[DatasetSummary, ...]:
    raw_sources = full_summary.get("datasets")
    if not isinstance(raw_sources, list):
        raise ReportContractError("datasets_invalid")
    sources = [_mapping(source, "dataset_invalid") for source in raw_sources]
    if agricultural_metadata:
        sources.append(_mapping(agricultural_metadata.get("source"), "agricultural_source_invalid"))
    unique: dict[str, DatasetSummary] = {}
    for source in sources:
        dataset_id = _contract_required_string(
            source.get("dataset_id", source.get("source_id")), "dataset_id_invalid"
        )
        unique[dataset_id] = DatasetSummary(
            dataset_id=dataset_id,
            provider=_contract_required_string(source.get("provider"), "provider_invalid"),
            collection=_contract_required_string(source.get("collection"), "collection_invalid"),
            version=_contract_required_string(source.get("version"), "dataset_version_invalid"),
            access_date=_required_date(source.get("access_date"), "dataset_access_date_invalid"),
            license=_contract_required_string(source.get("license"), "dataset_license_invalid"),
            spatial_resolution_m=_optional_float(
                source.get("spatial_resolution_m"), "dataset_resolution_invalid"
            ),
        )
    return tuple(unique[key] for key in sorted(unique))


def _events(
    report_dataset: dict[str, Any],
    disturbance_summary: dict[str, Any],
    persistence_summary: dict[str, Any],
    attribution_summary: dict[str, Any],
    disturbance_spatial: dict[str, Any],
    attribution_spatial: dict[str, Any],
) -> tuple[EventSummary, ...]:
    selected_payloads = report_dataset.get(
        "selected_disturbances", report_dataset.get("selected_events")
    )
    if not isinstance(selected_payloads, list):
        raise ReportContractError("selected_events_invalid")
    for payload in selected_payloads:
        selected = _mapping(payload, "selected_event_invalid")
        selected_attribution = _optional_mapping(selected.get("attribution"))
        _reject_automatic_confirmation(selected_attribution)
    selection = _optional_mapping(
        report_dataset.get("disturbance_selection", report_dataset.get("event_selection"))
    )
    selected_ids = set(
        _string_list(selection.get("selected_candidate_ids", selection.get("selected_event_ids")))
    )
    if not selected_ids:
        selected_ids = {
            _contract_required_string(
                _mapping(payload, "selected_event_invalid").get(
                    "candidate_id", _mapping(payload, "selected_event_invalid").get("event_id")
                ),
                "candidate_id_invalid",
            )
            for payload in selected_payloads
        }

    disturbances = _indexed_events(disturbance_summary, "disturbance_events_invalid")
    persistence = (
        _indexed_events(persistence_summary, "persistence_events_invalid")
        if persistence_summary
        else {}
    )
    attributions = (
        _attribution_records(attribution_summary, "attribution_records_invalid")
        if attribution_summary
        else [_unattributed_candidate(item) for item in disturbances.values()]
    )
    event_geometries = _feature_geometries(disturbance_spatial)
    attribution_geometries = _feature_geometries(attribution_spatial or {"features": []})
    agricultural_geometries = _agricultural_geometries(attribution_spatial or {"features": []})
    result: list[EventSummary] = []
    for ordinal, attribution in enumerate(attributions, start=1):
        _reject_automatic_confirmation(attribution)
        candidate_id = _attribution_candidate_id(attribution)
        record_type = _attribution_record_type(attribution)
        interpretation_level: Literal["candidate_episode", "event"] = (
            "event" if record_type == "conversion_likely_event" else "candidate_episode"
        )
        interpretation_status = _attribution_interpretation_status(attribution)
        event_id = candidate_id if record_type == "conversion_likely_event" else None
        disturbance = disturbances.get(candidate_id, {})
        persistent = persistence.get(candidate_id, {})
        sensitivity = persistent.get("sensitivity")
        sensitivity_areas = (
            [
                _required_float(item.get("persistent_crop_area_ha"), "sensitivity_area_invalid")
                for item in sensitivity
                if isinstance(item, dict)
            ]
            if isinstance(sensitivity, list)
            else []
        )
        gates = _mapping(attribution.get("gates"), "event_gates_invalid")
        trajectory = _mapping(attribution.get("trajectory"), "event_trajectory_invalid")
        years = trajectory.get("years")
        fractions = trajectory.get("forest_fraction")
        if (
            not isinstance(years, list)
            or not isinstance(fractions, list)
            or len(years) != len(fractions)
        ):
            raise ReportContractError("event_trajectory_invalid")
        quality_flags = tuple(
            dict.fromkeys(
                [
                    *_string_list(attribution.get("quality_flags")),
                    *_string_list(persistent.get("quality_flags")),
                ]
            )
        )
        candidate_geometry = _geometry_rings(attribution.get("candidate_geometry")) or (
            event_geometries.get(candidate_id, ())
        )
        likely_geometry = _geometry_rings(
            attribution.get("likely_conversion_geometry")
        ) or attribution_geometries.get(candidate_id, ())
        display_geometry = (
            likely_geometry
            if record_type == "conversion_likely_event" and likely_geometry
            else candidate_geometry
        )
        evidence_gates = [
            EvidenceGate(key=key, passed=_required_bool(gates.get(key), "gate_invalid"))
            for key in _GATE_KEYS
        ]
        if _OPERATIONAL_AREA_GATE in gates:
            evidence_gates.append(
                EvidenceGate(
                    key=_OPERATIONAL_AREA_GATE,
                    passed=_required_bool(gates.get(_OPERATIONAL_AREA_GATE), "gate_invalid"),
                )
            )
        result.append(
            EventSummary(
                ordinal=ordinal,
                candidate_id=candidate_id,
                event_id=event_id,
                record_type=record_type,
                interpretation_level=interpretation_level,
                interpretation_status=interpretation_status,
                selected_for_detail=candidate_id in selected_ids,
                automatic_status=_contract_required_string(
                    attribution.get("automatic_status"), "automatic_status_invalid"
                ),
                area_ha=_required_float(
                    attribution.get(
                        "candidate_area_ha",
                        attribution.get("area_ha", disturbance.get("area_ha")),
                    ),
                    "event_area_invalid",
                ),
                area_threshold_ha=_required_float(
                    disturbance.get("area_threshold_ha", 0.5), "event_threshold_invalid"
                ),
                area_threshold_met=attribution.get(
                    "area_threshold_met", disturbance.get("area_threshold_met")
                )
                is True,
                likely_conversion_area_ha=_required_float(
                    attribution.get("likely_conversion_area_ha", 0.0),
                    "likely_conversion_area_invalid",
                ),
                conjunctive_conversion_evidence_area_ha=_required_float(
                    attribution.get(
                        "conjunctive_conversion_evidence_area_ha",
                        attribution.get("likely_conversion_area_ha", 0.0),
                    ),
                    "conjunctive_conversion_evidence_area_invalid",
                ),
                estimated_onset_period_id=_contract_required_string(
                    attribution.get(
                        "estimated_onset_period_id",
                        disturbance.get("estimated_onset_period_id"),
                    ),
                    "event_onset_invalid",
                ),
                estimated_onset_window_start=_optional_date(
                    disturbance.get("estimated_onset_window_start"),
                    "event_onset_start_invalid",
                ),
                estimated_onset_window_end=_optional_date(
                    disturbance.get("estimated_onset_window_end"),
                    "event_onset_end_invalid",
                ),
                post_change_use=_contract_required_string(
                    attribution.get("post_change_use", "unknown"), "post_change_use_invalid"
                ),
                confidence=_optional_float(attribution.get("confidence"), "confidence_invalid"),
                confidence_semantics=_optional_contract_string(
                    attribution.get("confidence_semantics"), "confidence_semantics_invalid"
                ),
                human_review_required=attribution.get("human_review_required") is not False,
                observed_period_count=_optional_int(
                    persistent.get("valid_period_count"), "valid_period_count_invalid"
                ),
                qualifying_period_count=_optional_int(
                    persistent.get("qualifying_period_count"), "qualifying_period_count_invalid"
                ),
                gap_period_count=_optional_int(
                    persistent.get("gap_period_count"), "gap_period_count_invalid"
                ),
                persistent_agricultural_area_ha=_required_float(
                    attribution.get(
                        "persistent_agricultural_area_ha",
                        persistent.get("persistent_crop_area_ha", 0.0),
                    ),
                    "persistent_agricultural_area_invalid",
                ),
                persistent_event_fraction=_optional_float(
                    persistent.get("persistent_event_fraction"),
                    "persistent_event_fraction_invalid",
                ),
                sensitivity_min_area_ha=min(sensitivity_areas) if sensitivity_areas else None,
                sensitivity_max_area_ha=max(sensitivity_areas) if sensitivity_areas else None,
                dual_detector_pixel_count=_optional_int(
                    disturbance.get("dual_detector_agreement_pixel_count"),
                    "dual_detector_count_invalid",
                ),
                pixel_count=_optional_positive_int(
                    disturbance.get("pixel_count"), "pixel_count_invalid"
                ),
                gates=tuple(evidence_gates),
                forest_trajectory=tuple(
                    AnnualForestPoint(
                        year=_required_int(year, "trajectory_year_invalid"),
                        forest_fraction=_required_float(fraction, "trajectory_fraction_invalid"),
                    )
                    for year, fraction in zip(years, fractions, strict=True)
                ),
                quality_flags=quality_flags,
                event_geometry=display_geometry,
                candidate_geometry=candidate_geometry,
                agricultural_geometry=agricultural_geometries.get(candidate_id, ()),
            )
        )
    # Un run limpio sin registros de atribución es un resultado válido: el informe
    # debe poder documentar la ausencia de perturbaciones detectadas (low_risk).
    return tuple(result)


def _unattributed_candidate(disturbance: Mapping[str, Any]) -> dict[str, Any]:
    candidate_id = disturbance.get("candidate_id", disturbance.get("event_id"))
    return {
        "candidate_id": candidate_id,
        "record_type": "disturbance_candidate",
        "interpretation_status": "candidate_only",
        "automatic_status": "review_required",
        "area_ha": disturbance.get("area_ha"),
        "estimated_onset_period_id": disturbance.get("estimated_onset_period_id"),
        "post_change_use": "unknown",
        "human_review_required": True,
        "gates": {key: False for key in _GATE_KEYS},
        "trajectory": {"years": [], "forest_fraction": []},
        "quality_flags": ["post_change_attribution_unavailable"],
    }


def _reject_automatic_confirmation(attribution: dict[str, Any]) -> None:
    if (
        attribution.get("automatic_status") == "conversion_confirmed"
        or attribution.get("conversion_confirmed") is True
    ):
        raise ReportContractError("automatic_conversion_confirmed_forbidden")


def _event_list(payload: dict[str, Any], code: str) -> list[dict[str, Any]]:
    events = payload.get("events")
    if not isinstance(events, list):
        raise ReportContractError(code)
    return [_mapping(event, code) for event in events]


def _attribution_records(payload: dict[str, Any], code: str) -> list[dict[str, Any]]:
    """Lee el inventario neutral v4 y conserva compatibilidad con contratos previos."""
    records = payload.get("records")
    if records is None:
        records = payload.get("events")
    if not isinstance(records, list):
        raise ReportContractError(code)
    return [_mapping(record, code) for record in records]


def _attribution_candidate_id(attribution: Mapping[str, Any]) -> str:
    value = attribution.get("candidate_id", attribution.get("event_id"))
    return _contract_required_string(value, "candidate_id_invalid")


def _attribution_record_type(
    attribution: Mapping[str, Any],
) -> Literal["disturbance_candidate", "conversion_likely_event"]:
    value = attribution.get("record_type")
    interpretation_status = attribution.get("interpretation_status")
    automatic_status = attribution.get("automatic_status")
    if value == "disturbance_candidate":
        return "disturbance_candidate"
    if value == "conversion_likely_event":
        if interpretation_status not in {None, "conversion_likely"}:
            return "disturbance_candidate"
        if automatic_status not in {None, "conversion_likely"}:
            return "disturbance_candidate"
        return "conversion_likely_event"
    return (
        "conversion_likely_event"
        if automatic_status == "conversion_likely"
        and interpretation_status in {None, "conversion_likely"}
        else "disturbance_candidate"
    )


def _attribution_interpretation_status(
    attribution: Mapping[str, Any],
) -> Literal[
    "candidate_only",
    "temporary_or_recovered",
    "persistent_unattributed",
    "insufficient_data",
    "subthreshold_conversion_evidence",
    "conversion_likely",
]:
    value = attribution.get("interpretation_status")
    allowed = {
        "candidate_only",
        "temporary_or_recovered",
        "persistent_unattributed",
        "insufficient_data",
        "subthreshold_conversion_evidence",
        "conversion_likely",
    }
    is_conversion_event = _attribution_record_type(attribution) == "conversion_likely_event"
    if value in allowed and (value != "conversion_likely" or is_conversion_event):
        return cast(
            Literal[
                "candidate_only",
                "temporary_or_recovered",
                "persistent_unattributed",
                "insufficient_data",
                "subthreshold_conversion_evidence",
                "conversion_likely",
            ],
            value,
        )
    if attribution.get("automatic_status") == "conversion_likely" and is_conversion_event:
        return "conversion_likely"
    if attribution.get("automatic_status") == "insufficient_data":
        return "insufficient_data"
    if attribution.get("post_change_use") in {
        "forest_recovery",
        "managed_harvest",
        "temporary_disturbance",
    }:
        return "temporary_or_recovered"
    gates = attribution.get("gates")
    if isinstance(gates, Mapping) and gates.get("persistent_change") is True:
        return "persistent_unattributed"
    return "candidate_only"


def _indexed_events(payload: dict[str, Any], code: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for event in _event_list(payload, code):
        event_id = _contract_required_string(event.get("event_id"), "event_id_invalid")
        result[event_id] = event
    return result


def _feature_geometries(
    payload: dict[str, Any],
) -> dict[str, tuple[tuple[tuple[float, float], ...], ...]]:
    features = payload.get("features")
    if not isinstance(features, list):
        raise ReportContractError("spatial_features_invalid")
    result: dict[str, tuple[tuple[tuple[float, float], ...], ...]] = {}
    for feature in features:
        item = _mapping(feature, "spatial_feature_invalid")
        properties = _mapping(item.get("properties"), "spatial_properties_invalid")
        candidate_id = _contract_required_string(
            properties.get("candidate_id", properties.get("event_id")),
            "candidate_id_invalid",
        )
        result[candidate_id] = _geometry_rings(item.get("geometry"))
    return result


def _agricultural_geometries(
    payload: dict[str, Any],
) -> dict[str, tuple[tuple[tuple[float, float], ...], ...]]:
    features = payload.get("features")
    if not isinstance(features, list):
        raise ReportContractError("attribution_spatial_features_invalid")
    result: dict[str, tuple[tuple[tuple[float, float], ...], ...]] = {}
    for feature in features:
        item = _mapping(feature, "attribution_spatial_feature_invalid")
        properties = _mapping(item.get("properties"), "attribution_spatial_properties_invalid")
        event_id = _contract_required_string(
            properties.get("candidate_id", properties.get("event_id")),
            "event_id_invalid",
        )
        evidence = properties.get("agricultural_use_evidence")
        rings: list[tuple[tuple[float, float], ...]] = []
        if isinstance(evidence, list):
            for record in evidence:
                if not isinstance(record, dict):
                    continue
                observation = _optional_mapping(record.get("observation"))
                rings.extend(_geometry_rings(observation.get("geometry")))
        result[event_id] = tuple(rings)
    return result


def _geometry_rings(value: object) -> tuple[tuple[tuple[float, float], ...], ...]:
    if value is None:
        return ()
    geometry = _mapping(value, "geometry_invalid")
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Polygon" and isinstance(coordinates, list):
        polygons = [coordinates]
    elif geometry_type == "MultiPolygon" and isinstance(coordinates, list):
        polygons = coordinates
    else:
        raise ReportContractError("geometry_invalid")
    rings: list[tuple[tuple[float, float], ...]] = []
    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon:
            continue
        outer = polygon[0]
        if not isinstance(outer, list):
            continue
        points: list[tuple[float, float]] = []
        for coordinate in outer:
            if not isinstance(coordinate, list) or len(coordinate) < 2:
                raise ReportContractError("geometry_coordinate_invalid")
            points.append(
                (
                    _required_float(coordinate[0], "geometry_coordinate_invalid", signed=True),
                    _required_float(coordinate[1], "geometry_coordinate_invalid", signed=True),
                )
            )
        if len(points) >= 3:
            rings.append(tuple(points))
    return tuple(rings)


def _verified_object(package: ReportPackage, report_path: str) -> dict[str, Any]:
    asset = _verified_asset(package, report_path)
    return _load_object(asset.absolute_path, "verified_object_invalid")


def _verified_csv(package: ReportPackage, report_path: str) -> list[dict[str, str]]:
    asset = _verified_asset(package, report_path)
    try:
        with asset.absolute_path.open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))
    except (OSError, csv.Error) as error:
        raise ReportContractError("verified_csv_invalid") from error


def _verified_asset(package: ReportPackage, report_path: str) -> VerifiedReportAsset:
    for asset in package.assets:
        if asset.report_path == report_path:
            return asset
    raise ReportContractError("required_report_asset_missing")


def _mapping(value: object, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReportContractError(code)
    return value


def _optional_mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _required_bool(value: object, code: str) -> bool:
    if not isinstance(value, bool):
        raise ReportContractError(code)
    return value


def _required_float(value: object, code: str, *, signed: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReportContractError(code)
    result = float(value)
    if not signed and result < 0:
        raise ReportContractError(code)
    return result


def _optional_float(value: object, code: str) -> float | None:
    if value is None:
        return None
    return _required_float(value, code)


def _required_int(value: object, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReportContractError(code)
    return value


def _optional_int(value: object, code: str) -> int | None:
    if value is None:
        return None
    result = _required_int(value, code)
    if result < 0:
        raise ReportContractError(code)
    return result


def _optional_positive_int(value: object, code: str) -> int | None:
    result = _optional_int(value, code)
    if result is not None and result <= 0:
        raise ReportContractError(code)
    return result


def _optional_contract_string(value: object, code: str) -> str | None:
    if value is None:
        return None
    return _contract_required_string(value, code)


def _csv_float(value: object, code: str) -> float:
    if not isinstance(value, str):
        raise ReportContractError(code)
    try:
        result = float(value)
    except ValueError as error:
        raise ReportContractError(code) from error
    if result < 0:
        raise ReportContractError(code)
    return result


def _csv_bool(value: object) -> bool:
    return isinstance(value, str) and value.strip().casefold() in {"true", "1", "yes"}


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _contract_required_string(value: object, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReportContractError(code)
    return value


def _required_date(value: object, code: str) -> date:
    text = _contract_required_string(value, code)
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise ReportContractError(code) from error


def _optional_date(value: object, code: str) -> date | None:
    if value is None:
        return None
    return _required_date(value, code)


def _safe_file(root: Path, relative_value: str, label: str) -> Path:
    relative = Path(relative_value)
    candidate = (root / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or not candidate.is_relative_to(root):
        raise ReportIntegrityError(f"{label}_path_unsafe")
    if not candidate.is_file():
        raise ReportIntegrityError(f"{label}_missing")
    return candidate


def _required_string(value: object, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReportIntegrityError(code)
    return value


def _optional_string(value: object, code: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ReportIntegrityError(code)
    return value


def _required_sha256(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ReportIntegrityError(code)
    return value


def _load_object(path: Path, code: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReportIntegrityError(code) from error
    if not isinstance(payload, dict):
        raise ReportIntegrityError(code)
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
