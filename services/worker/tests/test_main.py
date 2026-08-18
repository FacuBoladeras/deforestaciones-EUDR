"""Pruebas del entrypoint sin iniciar un loop real."""

from __future__ import annotations

from typing import Any

import pytest

from deforestation_worker import __main__


@pytest.mark.parametrize(("has_job", "expected"), [(True, 0), (False, 2)])
def test_once_exit_code_reflects_whether_a_job_was_processed(
    monkeypatch: pytest.MonkeyPatch, has_job: bool, expected: int
) -> None:
    events: list[str] = []

    class FakeSettings:
        @classmethod
        def from_environment(cls) -> object:
            return object()

    class FakeWorker:
        def __init__(self, _settings: Any) -> None:
            pass

        def initialize(self) -> int:
            events.append("initialized")
            return 0

        def run_once(self) -> bool:
            events.append("run_once")
            return has_job

    monkeypatch.setattr(__main__, "WorkerSettings", FakeSettings)
    monkeypatch.setattr(__main__, "AnalysisWorker", FakeWorker)

    assert __main__.main(["--once"]) == expected
    assert events == ["initialized", "run_once"]


def test_forever_mode_delegates_initialization_to_worker_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeSettings:
        @classmethod
        def from_environment(cls) -> object:
            return object()

    class FakeWorker:
        def __init__(self, _settings: Any) -> None:
            pass

        def initialize(self) -> int:
            events.append("unexpected_initialize")
            return 0

        def run_forever(self) -> None:
            events.append("run_forever")

    monkeypatch.setattr(__main__, "WorkerSettings", FakeSettings)
    monkeypatch.setattr(__main__, "AnalysisWorker", FakeWorker)

    assert __main__.main([]) == 0
    assert events == ["run_forever"]
