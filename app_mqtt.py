import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from commands.arm_actions.actions import ArmActions
from commands.face_tracking.controller import FaceTrackingController

try:
    import Arm_Lib  # type: ignore
except Exception:
    Arm_Lib = None  # type: ignore

try:
    import paho.mqtt.client as mqtt  # type: ignore
except Exception as e:  # pragma: no cover
    raise SystemExit(
        "paho-mqtt is required. Install with: pip install paho-mqtt"
    ) from e


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CarebotAppMQTT:
    def __init__(self, config_path: str):
        # 로깅 설정
        if not logging.getLogger().handlers:
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            )
        self.log = logging.getLogger("carebot.app.mqtt")

        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # 전체 구성 보관 (하위 모듈로 전달)
        self._config = dict(cfg)

        # MQTT 구성
        self.mqtt_host = cfg.get("mqtt_host", "192.168.91.1")
        self.mqtt_port = int(cfg.get("mqtt_port", 1883))
        self.mqtt_base = cfg.get("mqtt_base", "carebot")
        self.mqtt_qos = int(cfg.get("mqtt_qos", 0))

        # 다중 로봇 구분용 ID (config)
        self.robot_id = cfg.get("robot_id", "robot_left")

        # Arm 시리얼 포트(by-path 권장): 단일 키 > 좌/우 전용 키 순으로 매핑
        # 예: /dev/serial/by-path/pci-0000:03:00.0-usb-0:1.2:1.0-port0
        self.arm_port = cfg.get("arm_port") or (
            cfg.get("arm_port_left")
            if self.robot_id == "robot_left"
            else cfg.get("arm_port_right")
        )
        if isinstance(self.arm_port, str):
            self.arm_port = self.arm_port.strip() or None

        self.topic_frontend_rx = f"{self.mqtt_base}/frontend/rx"
        self.topic_frontend_tx = f"{self.mqtt_base}/frontend/tx"
        self.topic_carebot_rx = (
            f"{self.mqtt_base}/carebot/rx"  # 백엔드에서 오는 명령 수신
        )
        self.topic_carebot_tx = (
            f"{self.mqtt_base}/carebot/tx"  # 백엔드로 이벤트/결과 전송
        )

        camera_index = int(cfg.get("camera_index", 0))
        update_interval_ms = int(cfg.get("update_interval_ms", 200))

        # 로봇팔 및 컨트롤러 초기화
        self.arm = None
        self._arm_io_lock = threading.Lock()
        self._last_manual_ts = 0.0
        try:
            if Arm_Lib is not None:
                if self.arm_port:
                    # Arm_Lib 구현에 따라 키워드/위치 인자 모두 시도
                    try:
                        self.arm = Arm_Lib.Arm_Device(port=self.arm_port)  # type: ignore[arg-type]
                    except TypeError:
                        try:
                            self.arm = Arm_Lib.Arm_Device(self.arm_port)  # type: ignore[misc]
                        except Exception:
                            self.arm = Arm_Lib.Arm_Device()
                else:
                    self.arm = Arm_Lib.Arm_Device()
        except Exception:
            self.arm = None

        try:
            self.actions = (
                ArmActions(
                    arm_device=self.arm,
                    arm_lock=self._arm_io_lock,
                    robot_id=self.robot_id,
                    config=self._config,
                )
                if self.arm is not None
                else None
            )
            if self.actions is not None:
                self.actions.set_ready_pose()
        except Exception:
            self.actions = None

        self._cmd_lock = threading.Lock()
        self._current_cmd = None
        self._action_thread = None
        self._action_cancel = None

        self.face_tracking: Optional[FaceTrackingController] = None
        if self.arm is not None:
            self.face_tracking = FaceTrackingController(
                arm_device=self.arm,
                camera_index=camera_index,
                update_interval_ms=update_interval_ms,
            )
            self.face_tracking.set_callback(self._on_face_tracking_event)
            try:
                self.face_tracking.set_arm_io_lock(self._arm_io_lock)
            except Exception:
                pass

        self.log.info(
            "initialized | mqtt=%s:%s base=%s, cam=%s, interval_ms=%s, arm=%s, port=%s",
            self.mqtt_host,
            self.mqtt_port,
            self.mqtt_base,
            camera_index,
            update_interval_ms,
            "yes" if self.arm is not None else "no",
            self.arm_port or "default",
        )
        if self.arm is not None:
            self._start_joint_stream(interval_ms=update_interval_ms)

        # MQTT 클라이언트 (paho-mqtt 2.x 권장 콜백 API 사용, 하위호환 처리)
        # 인스턴스마다 고유 client_id 사용 (동일 ID 중복 접속 시 기존 연결이 끊김)
        client_id = f"carebot-app-{self.robot_id}-{os.getpid()}"
        try:
            self.client = mqtt.Client(
                client_id=client_id,
                clean_session=True,
                protocol=getattr(mqtt, "MQTTv311", 4),
                transport="tcp",
                callback_api_version=getattr(getattr(mqtt, "CallbackAPIVersion", None), "VERSION2", None),  # type: ignore[arg-type]
            )
        except TypeError:
            # 구버전 호환
            self.client = mqtt.Client(
                client_id=client_id,
                clean_session=True,
            )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

    # -------------- MQTT 콜백 --------------
    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: dict,
        rc: int,
        properties: Optional[Any] = None,
    ):
        self.log.info("mqtt connected rc=%s", rc)
        client.subscribe(self.topic_carebot_rx, qos=self.mqtt_qos)
        # hello + capabilities 전송
        capabilities = []
        if self.face_tracking is not None:
            capabilities.append("face_tracking")
        if self.actions is not None:
            capabilities += ["make_heart", "hug", "init_pose", "manual_control"]
        self._send(
            {
                "type": "hello",
                "ts": now_iso(),
                "agent": "carebot",
                "robot_id": self.robot_id,
                "capabilities": capabilities,
            }
        )

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        rc: Any = None,
        properties: Optional[Any] = None,
        *args: Any,
        **kwargs: Any,
    ):
        # paho-mqtt v1/v2 모두 호환: 추가 인수 무시, rc가 ReasonCode일 수도 있음
        try:
            rc_repr = getattr(rc, "value", rc)
        except Exception:
            rc_repr = rc
        self.log.info("mqtt disconnected rc=%s", rc_repr)

    def _on_message(self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage):
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            self._send({"type": "error", "ts": now_iso(), "error": "invalid_json"})
            return
        self.log.info("mqtt recv on %s: %s", msg.topic, data)

        # 다중 로봇: robot_id가 지정되어 있고, 내 로봇이 아니면 무시 (broadcast: 'all' 허용)
        rid = data.get("robot_id")
        if rid not in (None, "", self.robot_id, "all"):
            return

        # 명령만 수락 (WS 로직과 동일)
        msg_type = data.get("type")
        if msg_type is not None and msg_type != "command":
            if msg_type == "error":
                self.log.warning("mqtt error payload: %s", data)
            elif msg_type == "server_dispatch":
                self.log.debug("ignore server_dispatch: %s", data)
            return

        cmd = (data.get("command") or "").strip()
        if not cmd:
            self._send({"type": "error", "ts": now_iso(), "error": "missing_command"})
            return

        # ack 전송
        self._send(
            {"type": "ack", "ts": now_iso(), "command": cmd, "status": "accepted"}
        )

        # 선점(기존 동작 중단)
        self.log.info("preempt then dispatch | command=%s", cmd)
        self._preempt_current()

        start_tracking_cmds = {
            "face_tracking",
            "face_tracking_mode",
            "face_tracking_모드",
        }
        stop_tracking_cmds = {"stop_face_tracking", "stop_face_tracking_mode"}

        if cmd in start_tracking_cmds:
            self._cmd_start_face_tracking(cmd)
            return
        if cmd in stop_tracking_cmds:
            self._cmd_stop_face_tracking(cmd)
            return
        if cmd == "make_heart":
            self._cmd_make_heart(cmd)
            return
        if cmd in {"hug", "make_hug"}:
            self._cmd_hug(cmd)
            return
        if cmd in {"init_pose", "init", "ready_pose"}:
            self._cmd_init_pose(cmd)
            return
        if cmd == "set_joint":
            self._cmd_set_joint(cmd, data)
            return
        if cmd == "set_joints":
            self._cmd_set_joints(cmd, data)
            return
        if cmd == "nudge_joint":
            self._cmd_nudge_joint(cmd, data)
            return
        self._send(
            {
                "type": "error",
                "ts": now_iso(),
                "error": "unknown_command",
                "command": cmd,
            }
        )

    # -------------- 명령 헬퍼 (WS 앱과 동일) --------------
    def _preempt_current(self):
        with self._cmd_lock:
            try:
                if self.face_tracking is not None and self.face_tracking.is_running():
                    self.log.info("stopping face_tracking for preemption")
                    self.face_tracking.stop()
            except Exception:
                pass
            try:
                if self._action_thread is not None and self._action_thread.is_alive():
                    if self._action_cancel is None:
                        self._action_cancel = threading.Event()
                    self.log.info("cancelling running action thread")
                    self._action_cancel.set()
                    self._action_thread.join(timeout=2.0)
            except Exception:
                pass
            self._action_thread = None
            self._action_cancel = None
            self._current_cmd = None

    def _cmd_start_face_tracking(self, cmd: str):
        if self.face_tracking is None:
            self.log.warning("face_tracking unavailable")
            self._send(
                {
                    "type": "result",
                    "ts": now_iso(),
                    "command": cmd,
                    "status": "error",
                    "error": "arm_or_tracker_unavailable",
                }
            )
            return
        self.log.info("starting face_tracking")
        started = self.face_tracking.start()
        self._send(
            {
                "type": "result",
                "ts": now_iso(),
                "command": cmd,
                "status": (
                    "running"
                    if started or self.face_tracking.is_running()
                    else "already_running"
                ),
            }
        )

    def _cmd_stop_face_tracking(self, cmd: str):
        if self.face_tracking is None:
            self.log.warning("stop face_tracking but tracker unavailable")
            self._send(
                {
                    "type": "result",
                    "ts": now_iso(),
                    "command": cmd,
                    "status": "error",
                    "error": "tracker_unavailable",
                }
            )
            return
        self.log.info("stopping face_tracking")
        stopped = self.face_tracking.stop()
        self._send(
            {
                "type": "result",
                "ts": now_iso(),
                "command": cmd,
                "status": ("stopped" if stopped else "not_running"),
            }
        )

    def _cmd_make_heart(self, cmd: str):
        if self.actions is None:
            self._send(
                {
                    "type": "result",
                    "ts": now_iso(),
                    "command": cmd,
                    "status": "error",
                    "error": "arm_unavailable",
                }
            )
            return
        self._start_action(
            cmd, lambda cancel: self.actions.make_heart(cancel_event=cancel)
        )

    def _cmd_hug(self, cmd: str):
        if self.actions is None:
            self._send(
                {
                    "type": "result",
                    "ts": now_iso(),
                    "command": cmd,
                    "status": "error",
                    "error": "arm_unavailable",
                }
            )
            return
        self._start_action(cmd, lambda cancel: self.actions.hug(cancel_event=cancel))

    def _cmd_init_pose(self, cmd: str):
        if self.actions is None:
            self._send(
                {
                    "type": "result",
                    "ts": now_iso(),
                    "command": cmd,
                    "status": "error",
                    "error": "arm_unavailable",
                }
            )
            return
        self._start_action(
            cmd, lambda cancel: self.actions.init_pose(cancel_event=cancel)
        )

    def _cmd_set_joint(self, cmd: str, data: Dict[str, Any]):
        if self.actions is None:
            self._send(
                {
                    "type": "result",
                    "ts": now_iso(),
                    "command": cmd,
                    "status": "error",
                    "error": "arm_unavailable",
                }
            )
            return
        sid = data.get("id") or data.get("sid")
        angle = data.get("angle")
        t = data.get("time_ms", 500)
        try:
            sid_i = int(sid)
            if not 1 <= sid_i <= 6:
                raise ValueError("sid_out_of_range")
        except Exception:
            self._send(
                {
                    "type": "result",
                    "ts": now_iso(),
                    "command": cmd,
                    "status": "error",
                    "error": "invalid_sid",
                }
            )
            return
        if angle is None:
            self._send(
                {
                    "type": "result",
                    "ts": now_iso(),
                    "command": cmd,
                    "status": "error",
                    "error": "missing_angle",
                }
            )
            return
        try:
            self._last_manual_ts = time.time()
        except Exception:
            pass
        self.log.info("set_joint request | sid=%s angle=%s t=%s", sid, angle, t)
        outcome = self.actions.set_joint(sid, angle, t)
        self._send(
            {
                "type": "result",
                "ts": now_iso(),
                "command": cmd,
                "status": ("ok" if outcome == "ok" else "error"),
                "outcome": outcome,
            }
        )
        self.log.info("set_joint outcome | %s", outcome)

    def _cmd_set_joints(self, cmd: str, data: Dict[str, Any]):
        if self.actions is None:
            self._send(
                {
                    "type": "result",
                    "ts": now_iso(),
                    "command": cmd,
                    "status": "error",
                    "error": "arm_unavailable",
                }
            )
            return
        angles = data.get("angles")
        t = data.get("time_ms", 500)
        try:
            self._last_manual_ts = time.time()
        except Exception:
            pass
        outcome = self.actions.set_joints(angles, t)
        self._send(
            {
                "type": "result",
                "ts": now_iso(),
                "command": cmd,
                "status": ("ok" if outcome == "ok" else "error"),
                "outcome": outcome,
            }
        )

    def _cmd_nudge_joint(self, cmd: str, data: Dict[str, Any]):
        if self.actions is None:
            self._send(
                {
                    "type": "result",
                    "ts": now_iso(),
                    "command": cmd,
                    "status": "error",
                    "error": "arm_unavailable",
                }
            )
            return
        sid = data.get("id") or data.get("sid")
        delta = data.get("delta", 0)
        t = data.get("time_ms", 300)
        try:
            sid_i = int(sid)
            if not 1 <= sid_i <= 6:
                raise ValueError("sid_out_of_range")
        except Exception:
            self._send(
                {
                    "type": "result",
                    "ts": now_iso(),
                    "command": cmd,
                    "status": "error",
                    "error": "invalid_sid",
                }
            )
            return
        try:
            int(delta)
        except Exception:
            self._send(
                {
                    "type": "result",
                    "ts": now_iso(),
                    "command": cmd,
                    "status": "error",
                    "error": "invalid_delta",
                }
            )
            return
        try:
            self._last_manual_ts = time.time()
        except Exception:
            pass
        outcome = self.actions.nudge_joint(sid, delta, t)
        self._send(
            {
                "type": "result",
                "ts": now_iso(),
                "command": cmd,
                "status": ("ok" if outcome == "ok" else "error"),
                "outcome": outcome,
            }
        )

    def _start_action(self, cmd_name: str, action_callable):
        with self._cmd_lock:
            cancel_event = threading.Event()
            self._action_cancel = cancel_event
            self._current_cmd = cmd_name

            def _runner():
                try:
                    self.log.info("action start | %s", cmd_name)
                    outcome = action_callable(cancel_event)
                    status = "completed" if not cancel_event.is_set() else "cancelled"
                    self._send(
                        {
                            "type": "result",
                            "ts": now_iso(),
                            "command": cmd_name,
                            "status": status,
                            "outcome": outcome,
                        }
                    )
                    self.log.info(
                        "action result | %s | %s (%s)", cmd_name, status, outcome
                    )
                except Exception as e:
                    self.log.exception("action error | %s | %s", cmd_name, e)
                    self._send(
                        {
                            "type": "result",
                            "ts": now_iso(),
                            "command": cmd_name,
                            "status": "error",
                            "error": str(e),
                        }
                    )
                finally:
                    with self._cmd_lock:
                        self._action_thread = None
                        self._action_cancel = None
                        self._current_cmd = None

            self._send(
                {
                    "type": "progress",
                    "ts": now_iso(),
                    "command": cmd_name,
                    "status": "started",
                }
            )
            t = threading.Thread(target=_runner, name=f"Action-{cmd_name}", daemon=True)
            self._action_thread = t
            t.start()

    # LED 제어 함수 제거됨

    def _on_face_tracking_event(self, event: dict):
        event = dict(event)
        event.setdefault("ts", now_iso())
        self._send(event)

    def _send(self, obj: Dict[str, Any]):
        try:
            payload = dict(obj)
            # 전송 메타: 항상 robot_id를 강제 주입하여 프런트/백엔드가 로봇을 식별 가능하게 함
            payload["who"] = payload.get("who") or "carebot"
            payload["robot_id"] = self.robot_id
            self.client.publish(
                self.topic_carebot_tx, json.dumps(payload), qos=self.mqtt_qos
            )
        except Exception:
            pass

    def _start_joint_stream(self, interval_ms: int = 200):
        if getattr(self, "_telemetry_thread", None):
            return
        self._telemetry_stop = threading.Event()
        self._telemetry_seq = 0

        def _loop():
            last = [None] * 6
            first_sent = False
            force_interval_s = 1.0
            last_force = 0.0
            min_delta = 1.0  # 도메인 노이즈 억제를 위한 최소 변화 각도(도)
            while not self._telemetry_stop.is_set():
                # 활동/유휴에 따라 샘플링 주기 가변
                now_t = time.time()
                is_active = (now_t - getattr(self, "_last_manual_ts", 0.0)) < 3.0
                sleep_sec = (
                    (interval_ms / 1000.0)
                    if is_active
                    else max(0.3, (interval_ms * 2) / 1000.0)
                )
                if self.arm is not None:

                    def _read_angles(force: bool = False) -> Optional[list]:
                        try:
                            res = []
                            acquired = False
                            if force:
                                acquired = self._arm_io_lock.acquire(timeout=0.2)
                            else:
                                acquired = self._arm_io_lock.acquire(blocking=False)
                            if not acquired:
                                return None
                            try:
                                for i in range(6):
                                    val = self.arm.Arm_serial_servo_read(i + 1)
                                    res.append(int(val) if val is not None else None)
                                    time.sleep(0.003)
                            finally:
                                try:
                                    self._arm_io_lock.release()
                                except Exception:
                                    pass
                            return res
                        except Exception:
                            return None

                    # 평시엔 non-blocking, 주기적으로 강제 스냅샷(blocking)
                    force = (now_t - last_force) >= force_interval_s
                    angles = _read_angles(force=force)
                    if angles is not None:
                        should_send = False
                        if not first_sent:
                            should_send = True
                        else:
                            # min_delta 기준 변화 체크
                            for i in range(6):
                                a_new = angles[i]
                                a_old = last[i]
                                if a_new is None or a_old is None:
                                    if a_new != a_old:
                                        should_send = True
                                        break
                                else:
                                    if abs(a_new - a_old) >= min_delta:
                                        should_send = True
                                        break
                            # 강제 스냅샷 타이밍에는 한번 보내기
                            if not should_send and force:
                                should_send = True
                        if should_send:
                            payload = {
                                "type": "joint_state",
                                "angles": angles,
                                "ts": now_iso(),
                                "robot_id": self.robot_id,
                                "seq": self._telemetry_seq,
                            }
                            # 텔레메트리는 retain=True 권장 (백엔드에서 retain 전달 필요)
                            self._send(payload)
                            self._telemetry_seq += 1
                            last = list(angles)
                            last_force = now_t
                            first_sent = True
                time.sleep(max(0.08, sleep_sec))

        t = threading.Thread(target=_loop, name="JointTelemetry", daemon=True)
        self._telemetry_thread = t
        t.start()

    def run(self):
        self.log.info(
            "connecting to mqtt://%s:%s base=%s",
            self.mqtt_host,
            self.mqtt_port,
            self.mqtt_base,
        )
        # 브로커가 아직 준비되지 않은 경우를 대비해 재시도 루프
        while True:
            try:
                self.client.connect(self.mqtt_host, self.mqtt_port, keepalive=30)
                break
            except Exception as e:
                self.log.warning("mqtt connect failed: %s | retry in 2s", e)
                time.sleep(2.0)
        try:
            self.client.loop_forever()
        except KeyboardInterrupt:
            # 정상 종료 처리
            try:
                if self.face_tracking is not None:
                    self.face_tracking.stop()
            except Exception:
                pass
            try:
                if getattr(self, "_telemetry_stop", None) is not None:
                    self._telemetry_stop.set()
            except Exception:
                pass


if __name__ == "__main__":
    base = os.path.dirname(os.path.abspath(__file__))
    app = CarebotAppMQTT(config_path=os.path.join(base, "config.json"))
    app.run()
