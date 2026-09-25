from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "run_local_stack.py"
SPEC = importlib.util.spec_from_file_location("run_local_stack", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_choose_node_prefers_supported_bundled_runtime_over_path_node_18(tmp_path: Path) -> None:
    path_node = tmp_path / "path" / "node.exe"
    bundled_node = tmp_path / "bundled" / "node.exe"
    path_node.parent.mkdir()
    bundled_node.parent.mkdir()
    path_node.touch()
    bundled_node.touch()

    selected = MODULE.choose_node(
        path_node=path_node,
        bundled_node=bundled_node,
        version_reader=lambda path: 18 if path == path_node else 24,
    )

    assert selected == bundled_node


def test_shared_environment_uses_one_private_database_storage_and_node_path(
    tmp_path: Path,
) -> None:
    node = tmp_path / "runtime" / "node.exe"
    node.parent.mkdir()
    node.touch()

    environment = MODULE.build_shared_environment(
        base_environment={"PATH": "existing", "SECRET_TOKEN": "kept-in-process"},
        state_root=tmp_path / "state",
        node_executable=node,
    )

    assert environment["DEFORESTATION_API_DATABASE"] == str(
        (tmp_path / "state" / "private" / "jobs.sqlite3").resolve()
    )
    assert environment["DEFORESTATION_API_STORAGE"] == str(
        (tmp_path / "state" / "private" / "objects").resolve()
    )
    assert environment["DEFORESTATION_ANALYSIS_OUTPUT_ROOT"] == str(
        (tmp_path / "state" / "runs").resolve()
    )
    assert environment["PATH"].split(os.pathsep)[0] == str(node.parent.resolve())
    assert environment["SECRET_TOKEN"] == "kept-in-process"


def test_process_plan_has_exactly_one_api_worker_and_web_process(tmp_path: Path) -> None:
    plan = MODULE.build_process_plan(
        project_root=tmp_path,
        uv_executable=Path("uv"),
        pnpm_executable=Path("pnpm"),
    )

    assert [item.name for item in plan] == ["api", "worker", "web"]
    assert sum("deforestation-worker" in item.command for item in plan) == 1
    assert plan[2].cwd == tmp_path / "apps" / "web"


def test_instance_guard_rejects_live_launcher_without_killing_it(tmp_path: Path) -> None:
    lock_path = tmp_path / "launcher.json"
    lock_path.write_text(json.dumps({"pid": 4242, "instance_id": "existing"}), encoding="utf-8")

    with pytest.raises(MODULE.StackAlreadyRunningError, match="4242"):
        MODULE.InstanceGuard.acquire(
            lock_path,
            pid=9999,
            instance_id="new",
            process_is_alive=lambda pid: pid == 4242,
        )

    assert json.loads(lock_path.read_text(encoding="utf-8"))["instance_id"] == "existing"


def test_instance_guard_replaces_stale_metadata_and_only_owner_releases(tmp_path: Path) -> None:
    lock_path = tmp_path / "launcher.json"
    lock_path.write_text(json.dumps({"pid": 111, "instance_id": "stale"}), encoding="utf-8")
    guard = MODULE.InstanceGuard.acquire(
        lock_path,
        pid=222,
        instance_id="current",
        process_is_alive=lambda _pid: False,
    )
    lock_path.write_text(json.dumps({"pid": 333, "instance_id": "replacement"}), encoding="utf-8")

    guard.release()

    assert lock_path.exists()


@pytest.mark.skipif(os.name != "nt", reason="regresión específica de Windows")
def test_process_is_alive_uses_win32_process_handle_instead_of_os_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_kill(_pid: int, _signal: int) -> None:
        raise AssertionError("os.kill(pid, 0) no es un chequeo fiable en Windows")

    monkeypatch.setattr(MODULE.os, "kill", forbidden_kill)

    assert MODULE._process_is_alive(os.getpid()) is True
    assert MODULE._process_is_alive(2_147_483_647) is False


def test_assert_ports_available_reports_conflict_without_touching_owner() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    try:
        with pytest.raises(MODULE.PortInUseError, match=str(port)):
            MODULE.assert_ports_available("127.0.0.1", [port])
    finally:
        listener.close()


def test_sanitize_log_text_removes_credentials_tokens_and_signed_query_values() -> None:
    raw = (
        "Authorization: Bearer abc.def token=secret "
        "https://example.test/file?X-Goog-Signature=deadbeef&ok=1 "
        '"private_key":"very-secret"'
    )

    sanitized = MODULE.sanitize_log_text(raw)

    assert "abc.def" not in sanitized
    assert "secret" not in sanitized
    assert "deadbeef" not in sanitized
    assert "very-secret" not in sanitized
    assert "[REDACTED]" in sanitized


def test_stop_children_only_terminates_tracked_processes_in_reverse_order() -> None:
    events: list[tuple[str, int]] = []

    class FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid
            self.returncode: int | None = None

        def poll(self):  # type: ignore[no-untyped-def]
            return self.returncode

        def terminate(self) -> None:
            events.append(("terminate", self.pid))
            self.returncode = 0

        def wait(self, timeout=None):  # type: ignore[no-untyped-def]
            events.append(("wait", self.pid))
            return 0

    MODULE.stop_children([FakeProcess(1), FakeProcess(2)], timeout_seconds=0.01)

    assert events == [("terminate", 2), ("wait", 2), ("terminate", 1), ("wait", 1)]


def test_readiness_requires_api_web_and_live_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status = 200

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_args):  # type: ignore[no-untyped-def]
            return None

    responses = iter([Response(), Response()])
    monkeypatch.setattr(MODULE.urllib.request, "urlopen", lambda *_args, **_kwargs: next(responses))
    worker = SimpleNamespace(poll=lambda: None)

    MODULE.verify_readiness(worker, timeout_seconds=0.1, sleep=lambda _seconds: None)

    failed_worker = SimpleNamespace(poll=lambda: 3)
    with pytest.raises(MODULE.ChildProcessError, match="worker"):
        MODULE.verify_readiness(failed_worker, timeout_seconds=0.1, sleep=lambda _seconds: None)
