from __future__ import annotations

import time
from email.message import Message
from typing import Any
from urllib.error import HTTPError

import pytest

from deforestation_pipeline import raster_products
from deforestation_pipeline.raster_products import RasterDownloadError


class _Response:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, maximum_bytes: int) -> bytes:
        return self._content[:maximum_bytes]


def test_signed_download_retries_one_transient_transfer_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_urlopen(_request: object, *, timeout: int) -> Any:
        nonlocal calls
        calls += 1
        assert timeout == 180
        if calls == 1:
            raise ConnectionResetError("transient")
        return _Response(b"geotiff")

    monkeypatch.setattr(raster_products, "urlopen", fake_urlopen)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    result = raster_products._fetch_url_bytes("https://example.test/signed", 100)

    assert result == b"geotiff"
    assert calls == 2


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (TimeoutError("timed out"), "timeout"),
        (
            HTTPError(
                "https://example.test/signed",
                503,
                "unavailable",
                hdrs=Message(),
                fp=None,
            ),
            "remote_server_error",
        ),
        (
            HTTPError(
                "https://example.test/signed",
                429,
                "rate limited",
                hdrs=Message(),
                fp=None,
            ),
            "quota_or_rate_limit",
        ),
    ],
)
def test_signed_download_reports_safe_transfer_category_after_retry(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_code: str,
) -> None:
    calls = 0

    def failing_urlopen(_request: object, *, timeout: int) -> Any:
        nonlocal calls
        calls += 1
        assert timeout == 180
        raise error

    monkeypatch.setattr(raster_products, "urlopen", failing_urlopen)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    with pytest.raises(RasterDownloadError) as captured:
        raster_products._fetch_url_bytes("https://example.test/signed", 100)

    assert captured.value.code == expected_code
    assert "example.test" not in str(captured.value)
    assert calls == 2
