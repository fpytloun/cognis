"""Minimal WebSocket endpoint with first-message auth."""

from __future__ import annotations

import asyncio

from fastapi import WebSocket, WebSocketDisconnect


async def handle_websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    timeout_seconds = getattr(websocket.app.state, "ws_auth_timeout_seconds", 10)
    try:
        first_message = await asyncio.wait_for(websocket.receive_json(), timeout=timeout_seconds)
    except TimeoutError:
        await websocket.close(code=4401, reason="Authentication timeout")
        return
    except WebSocketDisconnect:
        return

    if first_message.get("type") != "auth" or not isinstance(first_message.get("token"), str):
        await websocket.close(code=4401, reason="Authentication required")
        return

    try:
        websocket.app.state.auth_provider.verify_jwt(first_message["token"], audience=["cognis"])
    except Exception:
        await websocket.close(code=4401, reason="Invalid token")
        return

    await websocket.send_json({"type": "authenticated"})
    while True:
        try:
            message = await websocket.receive_json()
        except WebSocketDisconnect:
            return
        if message.get("type") == "ping":
            await websocket.send_json({"type": "pong"})
        else:
            await websocket.send_json(
                {
                    "type": "error",
                    "code": "not_implemented",
                    "message": "WebSocket chat arrives in later stages",
                    "recoverable": True,
                }
            )
