"""Controles ASGI aplicados antes de parsear contratos HTTP."""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestSizeLimitMiddleware:
    """Limita bytes reales aun si Content-Length falta o fue falsificado."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise ValueError("request_size_limit_must_be_positive")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        declared_length = _content_length(scope)
        if declared_length is None:
            await _send_error(scope, receive, send, 400, "invalid_content_length")
            return
        if declared_length > self.max_bytes:
            await _send_error(scope, receive, send, 413, "request_too_large")
            return

        messages: list[Message] = []
        received_bytes = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.disconnect":
                break
            if message["type"] != "http.request":
                continue
            received_bytes += len(message.get("body", b""))
            if received_bytes > self.max_bytes:
                await _send_error(scope, receive, send, 413, "request_too_large")
                return
            if not message.get("more_body", False):
                break

        replay = _MessageReplay(messages)
        await self.app(scope, replay, send)


class _MessageReplay:
    def __init__(self, messages: list[Message]) -> None:
        self._messages = iter(messages)

    async def __call__(self) -> Message:
        return next(self._messages, {"type": "http.disconnect"})


def _content_length(scope: Scope) -> int | None:
    values = [value for key, value in scope.get("headers", []) if key.lower() == b"content-length"]
    if not values:
        return 0
    try:
        parsed = int(values[-1])
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


async def _send_error(
    scope: Scope,
    receive: Receive,
    send: Send,
    status_code: int,
    code: str,
) -> None:
    messages = {
        "invalid_content_length": "Content-Length inválido",
        "request_too_large": "El cuerpo supera el límite permitido",
    }
    response = JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": messages[code], "details": []}},
    )
    await response(scope, receive, send)
