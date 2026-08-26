"""Pruebas del entrypoint sin iniciar un loop real."""

from __future__ import annotations

import json
import logging
from typing import Any, cast

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
    monkeypatch.setattr(__main__, "configure_logging", lambda: events.append("logging"))

    assert __main__.main(["--once"]) == expected
    assert events == ["logging", "initialized", "run_once"]


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
    monkeypatch.setattr(__main__, "configure_logging", lambda: events.append("logging"))

    assert __main__.main([]) == 0
    assert events == ["logging", "run_forever"]


def test_json_formatter_exposes_safe_structured_worker_context() -> None:
    record = logging.LogRecord(
        "deforestation_worker",
        logging.INFO,
        __file__,
        1,
        "worker_stage_completed",
        (),
        None,
    )
    context = cast(Any, record)
    context.analysis_id = "analysis-1"
    context.attempt = 2
    context.stage = "packaging"
    context.duration_seconds = 1.25
    context.safe_error_code = None

    payload = json.loads(__main__.WorkerJsonFormatter().format(record))

    assert payload == {
        "analysis_id": "analysis-1",
        "attempt": 2,
        "duration_seconds": 1.25,
        "event": "worker_stage_completed",
        "level": "INFO",
        "safe_error_code": None,
        "stage": "packaging",
    }


def test_json_formatter_uses_safe_defaults_without_worker_context() -> None:
    record = logging.LogRecord(
        "deforestation_worker",
        logging.WARNING,
        __file__,
        1,
        "worker_idle",
        (),
        None,
    )

    payload = json.loads(__main__.WorkerJsonFormatter().format(record))

    assert payload["event"] == "worker_idle"
    assert payload["level"] == "WARNING"
    assert payload["analysis_id"] is None
    assert payload["duration_seconds"] == 0.0


def test_configure_logging_installs_visible_json_handler() -> None:
    logger = logging.getLogger("deforestation_worker")
    previous_handlers = logger.handlers[:]
    previous_level = logger.level
    previous_propagate = logger.propagate
    try:
        __main__.configure_logging()

        assert logger.level == logging.INFO
        assert logger.propagate is False
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0].formatter, __main__.WorkerJsonFormatter)
    finally:
        logger.handlers.clear()
        logger.handlers.extend(previous_handlers)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
