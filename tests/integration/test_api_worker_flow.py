"""Gate multiproceso del flujo HTTP -> SQLite -> worker -> resultados."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest


def test_api_and_worker_complete_a_synthetic_analysis_across_processes(
    tmp_path: Path,
) -> None:
    support = Path(__file__).with_name("support")
    api_script = support / "synthetic_api.py"
    worker_script = support / "synthetic_worker.py"
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    boundary = tmp_path / "argentina-test.geojson"
    boundary.write_text(
        json.dumps(
            {
                "type": "Polygon",
                "coordinates": [
                    [
                        [-65.0, -35.0],
                        [-58.0, -35.0],
                        [-58.0, -28.0],
                        [-65.0, -28.0],
                        [-65.0, -35.0],
                    ]
                ],
            }
        ),
        encoding="utf-8",
    )
    private_root = tmp_path / "private"
    output_root = tmp_path / "runs"
    environment = {
        "DEFORESTATION_API_PRIVATE_ROOT": str(private_root),
        "DEFORESTATION_ANALYSIS_OUTPUT_ROOT": str(output_root),
        "DEFORESTATION_API_JURISDICTION": str(boundary),
        "DEFORESTATION_E2E_PORT": str(port),
    }
    process_environment = __import__("os").environ.copy()
    process_environment.update(environment)

    api = subprocess.Popen(
        [sys.executable, str(api_script)],
        env=process_environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_health(base_url, api)
        created = _request_json(
            f"{base_url}/api/v1/analyses",
            method="POST",
            headers={"Idempotency-Key": str(uuid4())},
            payload={
                "establishment_id": "synthetic-e2e",
                "geometry": {
                    "type": "Feature",
                    "properties": {},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [-60.3, -32.1],
                                [-60.2, -32.1],
                                [-60.2, -32.0],
                                [-60.3, -32.0],
                                [-60.3, -32.1],
                            ]
                        ],
                    },
                },
            },
        )
        analysis_id = created["analysis_id"]
        assert created["status"] == "queued"

        worker = subprocess.run(
            [sys.executable, str(worker_script)],
            env=process_environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert worker.returncode == 0, worker.stdout + worker.stderr

        status = _request_json(f"{base_url}{created['status_url']}")
        report = _request_json(f"{base_url}/api/v1/analyses/{analysis_id}/report")
        events = _request_json(f"{base_url}/api/v1/analyses/{analysis_id}/events")
        assets = _request_json(f"{base_url}/api/v1/analyses/{analysis_id}/assets")

        assert status["status"] == "completed"
        assert status["stage"] == "completed"
        assert report["analysis"]["analysis_id"] == analysis_id
        assert events["items"] == [{"event_id": "synthetic-event-1", "area_ha": 0.75}]
        assert assets["total"] == 2

        image = next(item for item in assets["items"] if item["media_type"] == "image/png")
        assert _request_bytes(f"{base_url}{image['download_url']}") == b"synthetic-png"
        assert _request_bytes(f"{base_url}/api/v1/analyses/{analysis_id}/report.pdf").startswith(
            b"%PDF-"
        )

        package_path = tmp_path / "download.zip"
        package_path.write_bytes(
            _request_bytes(f"{base_url}/api/v1/analyses/{analysis_id}/download")
        )
        with zipfile.ZipFile(package_path) as archive:
            assert {
                "run_manifest.json",
                "client_report/informe-tecnico.pdf",
                "client_report/report.json",
                "report_assets/index.json",
                "report_assets/report_dataset.json",
                "report_assets/data/main/events.json",
                "report_assets/figures/general/overview.png",
            } <= set(archive.namelist())
    finally:
        api.terminate()
        try:
            api.wait(timeout=10)
        except subprocess.TimeoutExpired:
            api.kill()
            api.wait(timeout=10)


def _free_port() -> int:
    with closing(socket.socket()) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def _wait_for_health(base_url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            pytest.fail(f"La API E2E terminó antes de iniciar:\n{output}")
        try:
            health = _request_json(f"{base_url}/health")
            if health.get("status") == "ok":
                return
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            time.sleep(0.1)
    pytest.fail("La API E2E no quedó disponible dentro del timeout")


def _request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request_headers = {"Accept": "application/json", **(headers or {})}
    body = None
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        decoded = json.loads(response.read())
    assert isinstance(decoded, dict)
    return decoded


def _request_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=10) as response:
        return cast(bytes, response.read())
