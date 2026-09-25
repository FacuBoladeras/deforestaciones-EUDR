from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "export_rf_to_gee_asset.py"
SPEC = importlib.util.spec_from_file_location("export_rf_to_gee_asset", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_tree_chunking_preserves_order_and_respects_payload_limit() -> None:
    trees = ["a" * 4, "b" * 4, "c" * 4, "d" * 4]

    chunks = MODULE.plan_tree_chunks(trees, max_payload_bytes=11)

    assert chunks == [[trees[0], trees[1]], [trees[2], trees[3]]]
    assert [tree for chunk in chunks for tree in chunk] == trees
    assert all(MODULE.encoded_tree_bytes(chunk) <= 11 for chunk in chunks)


def test_tree_chunking_rejects_one_tree_larger_than_limit() -> None:
    with pytest.raises(ValueError, match="árbol individual"):
        MODULE.plan_tree_chunks(["x" * 12], max_payload_bytes=10)


def test_pipeline_contract_requires_classification_serialization() -> None:
    MODULE.validate_pipeline_output_mode("classification")
    with pytest.raises(ValueError, match="classification"):
        MODULE.validate_pipeline_output_mode("probability")


def test_tree_multiset_fingerprint_is_order_independent_and_encoding_stable() -> None:
    first = MODULE.tree_multiset_sha256(["root\nleaf", "second#leaf"])
    second = MODULE.tree_multiset_sha256(["second\nleaf", "root#leaf"])

    assert first == second
    assert len(first) == 64
    assert first != MODULE.tree_multiset_sha256(["root\nleaf", "changed"])


class _FakeData:
    def __init__(self, assets: dict[str, str]) -> None:
        self.assets = dict(assets)
        self.operations: list[tuple[str, str, str | None]] = []
        self.rename_failures: set[tuple[str, str]] = set()
        self.delete_failures: set[str] = set()
        self.tasks: list[dict[str, object]] = []
        self.task_statuses: dict[str, list[dict[str, str]]] = {}

    def getAsset(self, asset_id: str) -> dict[str, str]:
        if asset_id not in self.assets:
            raise RuntimeError(f"Asset {asset_id} not found")
        return {"id": asset_id, "type": "TABLE", "value": self.assets[asset_id]}

    def renameAsset(self, source: str, destination: str) -> None:
        self.operations.append(("rename", source, destination))
        if (source, destination) in self.rename_failures:
            raise RuntimeError("simulated promotion failure")
        if source not in self.assets:
            raise RuntimeError(f"Asset {source} not found")
        if destination in self.assets:
            raise RuntimeError(f"Asset {destination} already exists")
        self.assets[destination] = self.assets.pop(source)

    def deleteAsset(self, asset_id: str) -> None:
        self.operations.append(("delete", asset_id, None))
        if asset_id in self.delete_failures:
            self.delete_failures.remove(asset_id)
            raise RuntimeError("simulated cleanup failure")
        if asset_id not in self.assets:
            raise RuntimeError(f"Asset {asset_id} not found")
        del self.assets[asset_id]

    def getTaskList(self) -> list[dict[str, object]]:
        return list(self.tasks)

    def getTaskStatus(self, task_id: str) -> list[dict[str, str]]:
        return self.task_statuses[task_id]


class _FakeEe:
    def __init__(self, assets: dict[str, str]) -> None:
        self.data = _FakeData(assets)


def test_run_scoped_temporary_ids_do_not_collide_between_retries() -> None:
    first = MODULE.run_scoped_asset_ids("projects/p/assets/model", "run_a", 2)
    second = MODULE.run_scoped_asset_ids("projects/p/assets/model", "run_b", 2)

    first_values = {first["staging"], first["backup"], *first["parts"]}
    second_values = {second["staging"], second["backup"], *second["parts"]}
    assert first_values.isdisjoint(second_values)
    assert first["staging"].endswith("__staging_run_a")
    assert first["parts"] == [
        "projects/p/assets/model__staging_run_a__part_001",
        "projects/p/assets/model__staging_run_a__part_002",
    ]
    assert MODULE.run_scoped_asset_ids("projects/p/assets/model", "single", 0)["parts"] == []


def test_task_description_is_unique_per_run_safe_and_never_matches_historical_task() -> None:
    base = "Descripción repetida / con espacios " + ("x" * 200)
    first = MODULE.make_task_description(base, "run_aaaaaaaaaaaa", "part_001")
    second = MODULE.make_task_description(base, "run_bbbbbbbbbbbb", "part_001")

    assert first != second
    assert "run_aaaaaaaaaaaa" in first
    assert "run_bbbbbbbbbbbb" in second
    assert len(first) <= MODULE.GEE_TASK_DESCRIPTION_MAX_CHARS
    assert len(second) <= MODULE.GEE_TASK_DESCRIPTION_MAX_CHARS
    assert MODULE.GEE_TASK_DESCRIPTION_PATTERN.fullmatch(first)
    assert MODULE.GEE_TASK_DESCRIPTION_PATTERN.fullmatch(second)

    historical_only = SimpleNamespace(
        data=SimpleNamespace(
            getTaskList=lambda: [
                {
                    "id": "historical",
                    "description": first,
                    "creation_timestamp_ms": 1,
                }
            ]
        )
    )
    assert MODULE.find_task_by_description(historical_only, second) is None

    historical_only.data.getTaskList = lambda: [
        {"id": "historical", "description": first, "creation_timestamp_ms": 1},
        {"id": "current", "description": second, "creation_timestamp_ms": 2},
    ]
    assert MODULE.find_task_by_description(historical_only, second)["id"] == "current"


def test_part_failure_cleanup_deletes_only_assets_owned_by_current_run() -> None:
    module = _FakeEe({"part_current": "new", "part_other_run": "other"})

    report = MODULE.cleanup_owned_assets(
        module, ["part_current", "staging_current", "part_current"]
    )

    assert report["deleted"] == ["part_current"]
    assert report["missing"] == ["staging_current"]
    assert report["errors"] == []
    assert module.data.assets == {"part_other_run": "other"}


def test_merge_failure_cleanup_is_partial_failure_safe_and_idempotent() -> None:
    module = _FakeEe({"part_1": "one", "part_2": "two", "staging": "partial"})
    module.data.delete_failures.add("part_2")

    first = MODULE.cleanup_owned_assets(module, ["part_1", "part_2", "staging"])
    second = MODULE.cleanup_owned_assets(module, ["part_1", "part_2", "staging"])
    third = MODULE.cleanup_owned_assets(module, ["part_1", "part_2", "staging"])

    assert first["deleted"] == ["part_1", "staging"]
    assert first["errors"][0]["asset_id"] == "part_2"
    assert second["deleted"] == ["part_2"]
    assert third["deleted"] == []
    assert sorted(third["missing"]) == ["part_1", "part_2", "staging"]


def test_existing_destination_without_overwrite_is_never_modified() -> None:
    module = _FakeEe({"destination": "old", "staging": "new"})

    with pytest.raises(FileExistsError, match="ya existe"):
        MODULE.promote_staging_asset(
            module=module,
            staging_asset_id="staging",
            destination_asset_id="destination",
            overwrite=False,
            run_token="abc",
            expected_tree_count=500,
            verify_asset=lambda _asset_id, _count: {"verified": True},
            owned_asset_ids=["staging"],
        )

    assert module.data.assets == {"destination": "old", "staging": "new"}
    assert module.data.operations == []


def test_successful_overwrite_promotes_verified_staging_and_cleans_backup() -> None:
    module = _FakeEe({"destination": "old", "staging": "new"})
    owned = ["staging"]

    result = MODULE.promote_staging_asset(
        module=module,
        staging_asset_id="staging",
        destination_asset_id="destination",
        overwrite=True,
        run_token="success",
        expected_tree_count=500,
        verify_asset=lambda asset_id, count: {
            "verified": module.data.assets[asset_id] == "new",
            "expected_tree_count": count,
        },
        owned_asset_ids=owned,
    )

    assert result["verification"]["verified"] is True
    assert module.data.assets == {
        "destination": "new",
        "destination__backup_success": "old",
    }
    assert "destination__backup_success" in owned

    cleanup = MODULE.cleanup_owned_assets(module, owned)

    assert cleanup["deleted"] == ["destination__backup_success"]
    assert cleanup["missing"] == ["staging"]
    assert module.data.assets == {"destination": "new"}


def test_overwrite_promotion_failure_rolls_back_original_destination() -> None:
    module = _FakeEe({"destination": "old", "staging": "new"})
    module.data.rename_failures.add(("staging", "destination"))
    owned = ["staging"]

    with pytest.raises(RuntimeError, match="promotion failure"):
        MODULE.promote_staging_asset(
            module=module,
            staging_asset_id="staging",
            destination_asset_id="destination",
            overwrite=True,
            run_token="abc",
            expected_tree_count=500,
            verify_asset=lambda _asset_id, _count: {"verified": True},
            owned_asset_ids=owned,
        )

    assert module.data.assets["destination"] == "old"
    assert module.data.assets["staging"] == "new"
    assert "destination__backup_abc" not in owned
    assert "destination__backup_abc" not in module.data.assets


def test_failed_verification_after_promotion_rolls_back_and_cleanup_is_safe() -> None:
    module = _FakeEe({"destination": "old", "staging": "new"})
    owned = ["staging"]

    with pytest.raises(RuntimeError, match="invalid staging"):
        MODULE.promote_staging_asset(
            module=module,
            staging_asset_id="staging",
            destination_asset_id="destination",
            overwrite=True,
            run_token="xyz",
            expected_tree_count=500,
            verify_asset=lambda _asset_id, _count: (_ for _ in ()).throw(
                RuntimeError("invalid staging after promotion")
            ),
            owned_asset_ids=owned,
        )

    assert module.data.assets == {"destination": "old"}
    cleanup = MODULE.cleanup_owned_assets(module, owned)
    assert cleanup["errors"] == []


def test_failed_rollback_preserves_backup_for_manual_recovery() -> None:
    module = _FakeEe({"destination": "old", "staging": "new"})
    module.data.rename_failures.update(
        {
            ("staging", "destination"),
            ("destination__backup_safe", "destination"),
        }
    )
    owned = ["staging"]

    with pytest.raises(RuntimeError, match="también el rollback"):
        MODULE.promote_staging_asset(
            module=module,
            staging_asset_id="staging",
            destination_asset_id="destination",
            overwrite=True,
            run_token="safe",
            expected_tree_count=500,
            verify_asset=lambda _asset_id, _count: {"verified": True},
            owned_asset_ids=owned,
        )

    assert module.data.assets["destination__backup_safe"] == "old"
    assert "destination__backup_safe" not in owned
    cleanup = MODULE.cleanup_owned_assets(module, owned)
    assert "destination__backup_safe" in module.data.assets
    assert cleanup["errors"] == []


def test_main_failure_executes_finally_cleanup_without_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_token = "run_cleanup_test"
    destination = "projects/p/assets/models/model"
    staging = f"{destination}__staging_{run_token}"
    args = SimpleNamespace(
        model_path=tmp_path / "model.joblib",
        project="p",
        asset_id="models/model",
        output_dir=tmp_path / "export",
        features_json=None,
        output_mode="classification",
        processes=1,
        description="descripción elegida por usuario",
        auth_mode=None,
        convert_only=False,
        wait=True,
        poll_seconds=1,
        overwrite=False,
        allow_large_model=False,
    )
    model = SimpleNamespace(estimators_=[object()], classes_=[0, 1], n_features_in_=1)
    fake_ee = _FakeEe({})
    fake_ee.data.task_statuses["current-task"] = [
        {"id": "current-task", "state": "FAILED", "error_message": "boom"}
    ]

    class _FakeMl:
        @staticmethod
        def rf_to_strings(*_args: object, **_kwargs: object) -> list[str]:
            return ["tree"]

        @staticmethod
        def trees_to_csv(trees: list[str], path: str) -> None:
            Path(path).write_text("\n".join(trees), encoding="utf-8")

        @staticmethod
        def export_trees_to_fc(_trees: list[str], asset_id: str, *, description: str) -> None:
            fake_ee.data.assets[asset_id] = "partial"
            fake_ee.data.tasks.append({"id": "current-task", "description": description})

    monkeypatch.setattr(MODULE, "parse_args", lambda: args)
    monkeypatch.setattr(
        MODULE,
        "load_model_and_features",
        lambda _path, _features: (model, ["feature"], {}),
    )
    monkeypatch.setattr(MODULE, "import_gee_modules", lambda: (fake_ee, _FakeMl))
    monkeypatch.setattr(MODULE, "initialize_earth_engine", lambda *_args: None)
    monkeypatch.setattr(MODULE, "ensure_project_folders", lambda *_args: None)
    monkeypatch.setattr(MODULE, "new_run_token", lambda: run_token)

    with pytest.raises(RuntimeError, match="terminó FAILED"):
        MODULE.main()

    assert staging not in fake_ee.data.assets
    run_record = json.loads(
        (args.output_dir / f"gee_export_run_{run_token}.json").read_text(encoding="utf-8")
    )
    assert run_record["status"] == "failed"
    assert run_record["effective_task_descriptions"] == [
        MODULE.make_task_description(args.description, run_token, "staging")
    ]
    assert run_record["cleanup"] == {
        "deleted": [staging],
        "missing": [],
        "errors": [],
    }
