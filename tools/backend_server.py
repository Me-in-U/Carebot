import asyncio
import json
import os
from typing import Any, Dict, Set

import websockets


class Hub:
    def __init__(self) -> None:
        self.carebots: Set[Any] = set()
        self.frontends: Set[Any] = set()
        self.roles: Dict[Any, str] = {}

    def register(self, ws: Any, role: str) -> None:
        self.roles[ws] = role
        if role == "carebot":
            self.carebots.add(ws)
        else:
            self.frontends.add(ws)
        print(f"[hub] register {role} -> {ws.remote_address}")

    def unregister(self, ws: Any) -> None:
        role = self.roles.pop(ws, None)
        if role == "carebot":
            self.carebots.discard(ws)
        elif role == "frontend":
            self.frontends.discard(ws)
        else:
            # unknown role
            self.carebots.discard(ws)
            self.frontends.discard(ws)
        print(f"[hub] unregister {role} -> {getattr(ws, 'remote_address', None)}")

    async def broadcast(
        self, targets: Set[Any], message: dict, hop_label: str | None = None
    ) -> None:
        if not targets:
            return
        payload = dict(message)
        if hop_label and "who_hop" not in payload:
            payload["who_hop"] = hop_label
        data = json.dumps(payload)
        await asyncio.gather(*[self._safe_send(ws, data) for ws in targets])

    async def _safe_send(self, ws: Any, data: str) -> None:
        try:
            await ws.send(data)
        except Exception as e:
            print(f"[hub] send error: {e}")


hub = Hub()


def _extract_ws_path(args: tuple[Any, ...]) -> tuple[Any, str | None]:
    if len(args) == 1:
        ws = args[0]
        return ws, getattr(ws, "path", None)
    elif len(args) >= 2:
        return args[0], args[1]
    return None, None


def _parse_json(raw: str) -> dict | None:
    try:
        return json.loads(raw)
    except Exception:
        return None


async def _process_message(ws: Any, msg: dict) -> None:
    # Registration (hello)
    if msg.get("type") == "hello":
        agent = (msg.get("agent") or "").lower()
        role = "carebot" if "carebot" in agent else "frontend"
        hub.register(ws, role)
        await hub._safe_send(
            ws,
            json.dumps(
                {"type": "hello_ack", "role": role, "who_hop": "backend->client"}
            ),
        )
        return

    role = hub.roles.get(ws, "unknown")

    # Routing: Only accept commands from frontend clients
    is_command = (msg.get("type") == "command") or (
        msg.get("type") in (None, "") and "command" in msg
    )
    if is_command and role == "frontend":
        cmd = str(msg.get("command") or "").strip()
        if not cmd:
            await hub._safe_send(
                ws, json.dumps({"type": "error", "error": "missing_command"})
            )
            return
        # Preserve all command parameters from the frontend (e.g., id, delta, angles, time_ms)
        payload = dict(msg)
        payload["type"] = "command"
        payload["command"] = cmd
        if "who" not in payload:
            payload["who"] = "frontend"
        await hub.broadcast(
            hub.carebots,
            payload,
            hop_label="backend->carebots",
        )
        await hub._safe_send(
            ws,
            json.dumps(
                {
                    "type": "server_dispatch",
                    "command": cmd,
                    "status": "sent_to_carebots",
                    "who_hop": "backend->frontend",
                }
            ),
        )
        return

    # Default: forward carebot messages to frontends; ignore other frontend chatter
    if role == "carebot":
        await hub.broadcast(hub.frontends, msg, hop_label="backend->frontends")


async def handler(*args: Any):
    """Compatibility handler for websockets 10.x (ws, path) and 11.x+ (ws)."""
    ws, path = _extract_ws_path(args)
    if ws is None:
        return
    # Accept only /ws path (matches Carebot config); be lenient for development ('/' or None)
    if path not in ("/ws", "/", None):
        await ws.close(code=1008, reason="invalid path")
        return

    try:
        async for raw in ws:
            # Log every received frame (raw) with peer and role
            try:
                role = hub.roles.get(ws, "unknown")
            except Exception:
                role = "unknown"
            print(f"[recv] {getattr(ws, 'remote_address', None)} {role}: {raw}")
            msg = _parse_json(raw)
            if msg is None:
                await hub._safe_send(
                    ws, json.dumps({"type": "error", "error": "invalid_json"})
                )
                continue
            await _process_message(ws, msg)

    except websockets.ConnectionClosed:
        pass
    finally:
        hub.unregister(ws)


async def main():
    host = os.getenv("CAREBOT_BACKEND_HOST", "0.0.0.0")
    port = int(os.getenv("CAREBOT_BACKEND_PORT", "8765"))
    print(f"[server] starting ws server on ws://{host}:{port}/ws")
    async with websockets.serve(handler, host, port, ping_interval=20, ping_timeout=10):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[server] stopped")
