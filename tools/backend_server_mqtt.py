import json
import os
import sys
import time
from typing import Any

try:
    import paho.mqtt.client as mqtt  # type: ignore
except Exception as e:
    print("paho-mqtt is required. pip install paho-mqtt", file=sys.stderr)
    raise


# WebSocket 백엔드의 라우팅 동작을 그대로 반영한 MQTT 기반 허브입니다.
#
# 토픽 (base = CAREBOT_MQTT_BASE, 기본값 'carebot'):
#   - 프런트엔드 -> 백엔드:   {base}/frontend/tx
#   - 백엔드 -> 프런트엔드:   {base}/frontend/rx
#   - 케어봇   -> 백엔드:     {base}/carebot/tx
#   - 백엔드 -> 케어봇:       {base}/carebot/rx
#
# 라우팅 규칙:
#   - 프런트엔드에서 온 메시지 중 '명령(command)'만 케어봇으로 전달합니다.
#   - 케어봇에서 온 모든 메시지/이벤트는 프런트엔드로 전달합니다.
#   - 'hello' 메시지를 받으면 반대편으로 'hello_ack'을 돌려줍니다.
class MQTTHub:

    def __init__(self) -> None:
        self.host = os.getenv("CAREBOT_MQTT_HOST", "127.0.0.1")
        self.port = int(os.getenv("CAREBOT_MQTT_PORT", "1883"))
        self.base = os.getenv("CAREBOT_MQTT_BASE", "carebot")
        self.qos = int(os.getenv("CAREBOT_MQTT_QOS", "0"))

        self.topic_frontend_tx = f"{self.base}/frontend/tx"
        self.topic_frontend_rx = f"{self.base}/frontend/rx"
        self.topic_carebot_tx = f"{self.base}/carebot/tx"
        self.topic_carebot_rx = f"{self.base}/carebot/rx"

        # paho-mqtt 2.0 이상에서는 Callback API v2를 권장함
        try:
            self.client = mqtt.Client(
                client_id=os.getenv("CAREBOT_MQTT_SERVER_ID", "carebot-backend"),
                clean_session=True,
                protocol=getattr(mqtt, "MQTTv311", 4),
                transport="tcp",
                callback_api_version=getattr(getattr(mqtt, "CallbackAPIVersion", None), "VERSION2", None),  # type: ignore[arg-type]
            )
        except TypeError:
            # 구버전 호환
            self.client = mqtt.Client(
                client_id=os.getenv("CAREBOT_MQTT_SERVER_ID", "carebot-backend"),
                clean_session=True,
            )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

    def start(self) -> None:
        print(
            f"[mqtt-backend] connecting to mqtt://{self.host}:{self.port} base={self.base}"
        )
        # 초기 연결 실패 시 재시도 (브로커가 아직 안 떠 있을 때 대비)
        while True:
            try:
                self.client.connect(self.host, self.port, keepalive=30)
                break
            except Exception as e:
                print(f"[mqtt-backend] connect failed: {e} | retry in 2s")
                time.sleep(2.0)
        self.client.loop_forever()

    # ---------------- MQTT 콜백 ----------------
    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: dict,
        rc: int,
        properties: Any | None = None,
    ):
        print(f"[mqtt-backend] connected rc={rc}")
        # 수신 스트림 구독
        client.subscribe(self.topic_frontend_tx, qos=self.qos)
        client.subscribe(self.topic_carebot_tx, qos=self.qos)
        print(
            f"[mqtt-backend] subscribed to: {self.topic_frontend_tx}, {self.topic_carebot_tx}"
        )

    def _on_disconnect(
        self, client: mqtt.Client, userdata: Any, rc: int, properties: Any | None = None
    ):
        print(f"[mqtt-backend] disconnected rc={rc}")

    def _safe_pub(self, topic: str, payload: dict):
        try:
            data = json.dumps(payload)
            self.client.publish(topic, data, qos=self.qos)
        except Exception as e:
            print(f"[mqtt-backend] publish error to {topic}: {e}")

    def _parse(self, raw: bytes) -> dict | None:
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    def _on_message(self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage):
        src = msg.topic
        try:
            payload = self._parse(msg.payload)
        except Exception:
            payload = None
        print(f"[recv] topic={src} payload={msg.payload!r}")

        if payload is None:
            err = {"type": "error", "error": "invalid_json"}
            # 가능한 한 발신 측으로 오류를 반사해서 알림
            if src == self.topic_frontend_tx:
                self._safe_pub(self.topic_frontend_rx, err)
            elif src == self.topic_carebot_tx:
                self._safe_pub(self.topic_carebot_rx, err)
            return

        # Hello 핸드셰이크
        if payload.get("type") == "hello":
            if src == self.topic_frontend_tx:
                self._safe_pub(
                    self.topic_frontend_rx,
                    {
                        "type": "hello_ack",
                        "role": "frontend",
                        "who_hop": "backend->client",
                    },
                )
            elif src == self.topic_carebot_tx:
                self._safe_pub(
                    self.topic_carebot_rx,
                    {
                        "type": "hello_ack",
                        "role": "carebot",
                        "who_hop": "backend->client",
                    },
                )
            return

        # 프런트엔드에서 온 메시지: 명령만 케어봇으로 전달
        if src == self.topic_frontend_tx:
            is_command = (payload.get("type") == "command") or (
                payload.get("type") in (None, "") and "command" in payload
            )
            if is_command:
                cmd = str(payload.get("command") or "").strip()
                if not cmd:
                    self._safe_pub(
                        self.topic_frontend_rx,
                        {"type": "error", "error": "missing_command"},
                    )
                    return
                out = dict(payload)
                out.setdefault("type", "command")
                out["command"] = cmd
                if "who" not in out:
                    out["who"] = "frontend"
                if "who_hop" not in out:
                    out["who_hop"] = "backend->carebots"
                self._safe_pub(self.topic_carebot_rx, out)
                # 프런트엔드로 server_dispatch 통지
                self._safe_pub(
                    self.topic_frontend_rx,
                    {
                        "type": "server_dispatch",
                        "command": cmd,
                        "status": "sent_to_carebots",
                        "who_hop": "backend->frontend",
                    },
                )
            # 명령이 아닌 프런트엔드 메시지는 무시
            return

        # 케어봇에서 온 메시지: 모두 프런트엔드로 전달
        if src == self.topic_carebot_tx:
            out = dict(payload)
            if "who_hop" not in out:
                out["who_hop"] = "backend->frontends"
            self._safe_pub(self.topic_frontend_rx, out)
            return


def main():
    hub = MQTTHub()
    try:
        hub.start()
    except KeyboardInterrupt:
        print("[mqtt-backend] stopped")


if __name__ == "__main__":
    main()
