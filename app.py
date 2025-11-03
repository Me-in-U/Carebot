import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from commands.arm_actions.actions import ArmActions
from commands.face_tracking.controller import FaceTrackingController

try:  # Import Arm_Lib and create a single shared Arm instance in app
    import Arm_Lib  # type: ignore
except Exception:
    Arm_Lib = None  # type: ignore

try:
    import websocket  # type: ignore
except Exception as e:  # pragma: no cover
    raise SystemExit("websocket-client is required. Install requirements.txt") from e


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CarebotApp:
    def __init__(self, config_path: str):
        # Setup basic logging once
        if not logging.getLogger().handlers:
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            )
        self.log = logging.getLogger("carebot.app")
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        ws_url = os.environ.get(
            "CAREBOT_WS_URL", cfg.get("ws_url", "ws://127.0.0.1:8765/ws")
        )
        camera_index = int(cfg.get("camera_index", 0))
        update_interval_ms = int(cfg.get("update_interval_ms", 200))

        self.ws_url = ws_url
        self.ws: Optional[websocket.WebSocketApp] = None

        # Controllers
        # Initialize a single Arm device at startup and share it across commands
        self.arm = None
        # Shared lock to serialize all low-level Arm I/O across threads
        self._arm_io_lock = threading.Lock()
        # Track manual control activity timestamps (optional for future heuristics)
        self._last_manual_ts = 0.0
        try:
            if Arm_Lib is not None:
                self.arm = Arm_Lib.Arm_Device()
        except Exception:
            self.arm = None

        try:
            self.actions = (
                ArmActions(arm_device=self.arm, arm_lock=self._arm_io_lock)
                if self.arm is not None
                else None
            )
            if self.actions is not None:
                # Move to a safe ready pose once at startup
                self.actions.set_ready_pose()
        except Exception:
            # Arm may be unavailable on this host; degrade gracefully
            self.actions = None
        self._lock = threading.Lock()
        # Command preemption state
        self._cmd_lock = threading.Lock()
        self._current_cmd = None
        self._action_thread = None
        self._action_cancel = None

        # Face tracking (camera+arm) controller
        self.face_tracking: Optional[FaceTrackingController] = None
        if self.arm is not None:
            self.face_tracking = FaceTrackingController(
                arm_device=self.arm,
                camera_index=camera_index,
                update_interval_ms=update_interval_ms,
            )
            self.face_tracking.set_callback(self._on_face_tracking_event)
            # Inject shared I/O lock to controller as well
            try:
                self.face_tracking.set_arm_io_lock(self._arm_io_lock)
            except Exception:
                pass
        self.log.info(
            "initialized | ws_url=%s, cam=%s, interval_ms=%s, arm=%s",
            self.ws_url,
            camera_index,
            update_interval_ms,
            "yes" if self.arm is not None else "no",
        )
        # Start telemetry stream if arm is available
        if self.arm is not None:
            self._start_joint_stream(interval_ms=update_interval_ms)

    # ----------------- WebSocket callbacks -----------------
    def _on_open(self, ws):
        capabilities = []
        if self.face_tracking is not None:
            capabilities.append("face_tracking")
        if self.actions is not None:
            capabilities.append("make_heart")
            capabilities.append("hug")
            capabilities.append("init_pose")
            capabilities.append("manual_control")
        self.log.info("ws open | capabilities=%s", capabilities)
        self._send(
            {
                "type": "hello",
                "ts": now_iso(),
                "agent": "carebot",
                "capabilities": capabilities,
            }
        )
        # Ensure telemetry is running after connection
        if self.arm is not None:
            try:
                self._start_joint_stream(interval_ms=200)
            except Exception:
                pass

    def _on_message(self, ws, message):
        self.log.info("ws recv: %s", message)
        try:
            data = json.loads(message)
        except Exception:
            self._send({"type": "error", "ts": now_iso(), "error": "invalid_json"})
            return
        # Ignore typed control messages from server/frontend (e.g., server_dispatch, hello_ack)
        msg_type = data.get("type")
        if msg_type is not None and msg_type != "command":
            # Optionally log and ignore
            if msg_type == "error":
                self.log.warning("ws error payload: %s", data)
            elif msg_type == "server_dispatch":
                # Frontend acknowledgement; not a command for the bot to execute
                self.log.debug("ignore server_dispatch: %s", data)
            return

        cmd = (data.get("command") or "").strip()
        if not cmd:
            self._send({"type": "error", "ts": now_iso(), "error": "missing_command"})
            return

        # Accept and dispatch
        self._send(
            {"type": "ack", "ts": now_iso(), "command": cmd, "status": "accepted"}
        )
        # LED: command received -> RED
        self._led_set(50, 0, 0)

        # Global preemption: stop any running command before starting a new one
        self.log.info("preempt then dispatch | command=%s", cmd)
        self._preempt_current()

        # Command dispatch table with synonyms
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

    # ----------------- Command handlers -----------------
    def _preempt_current(self):
        with self._cmd_lock:
            # Stop face tracking if running
            try:
                if self.face_tracking is not None and self.face_tracking.is_running():
                    self.log.info("stopping face_tracking for preemption")
                    self.face_tracking.stop()
            except Exception:
                pass
            # Cancel running action thread if any
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

            # no LED changes here; handled per-command

    def _stop_face_tracking_if_running(self):
        try:
            if self.face_tracking is not None and self.face_tracking.is_running():
                self.face_tracking.stop()
        except Exception:
            pass

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
            # LED: finished (error) -> BLUE
            self._led_set(0, 0, 50)
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
        # LED: running -> GREEN
        if started or self.face_tracking.is_running():
            self._led_set(0, 50, 0)

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
            # LED: finished (error) -> BLUE
            self._led_set(0, 0, 50)
            return
        self.log.info("stopping face_tracking")
        stopped = self.face_tracking.stop()
        self._send(
            {
                "type": "result",
                "ts": now_iso(),
                "command": cmd,
                "status": "stopped" if stopped else "not_running",
            }
        )
        # LED: finished -> BLUE
        self._led_set(0, 0, 50)

    def _cmd_make_heart(self, cmd: str):
        if self.actions is None:
            self.log.warning("make_heart but arm actions unavailable")
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
        # Run action in a dedicated thread with cooperative cancellation
        self._start_action(
            cmd, lambda cancel: self.actions.make_heart(cancel_event=cancel)
        )

    def _cmd_hug(self, cmd: str):
        if self.actions is None:
            self.log.warning("hug but arm actions unavailable")
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
            self.log.warning("init_pose but arm actions unavailable")
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

    # ---------- Manual control command handlers ----------
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
        # Basic validation to avoid int(None) issues downstream
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
        # mark recent manual activity
        try:
            self._last_manual_ts = time.time()
        except Exception:
            pass
        outcome = self.actions.set_joint(sid, angle, t)
        self._send(
            {
                "type": "result",
                "ts": now_iso(),
                "command": cmd,
                "status": "ok" if outcome == "ok" else "error",
                "outcome": outcome,
            }
        )

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
                "status": "ok" if outcome == "ok" else "error",
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
        # Validate inputs to prevent TypeErrors inside action
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
                "status": "ok" if outcome == "ok" else "error",
                "outcome": outcome,
            }
        )

    def _start_action(self, cmd_name: str, action_callable):
        """Start an action in a thread with cancellation support and send lifecycle events."""
        with self._cmd_lock:
            cancel_event = threading.Event()
            self._action_cancel = cancel_event
            self._current_cmd = cmd_name

            def _runner():
                try:
                    self.log.info("action start | %s", cmd_name)
                    # LED: running -> GREEN
                    self._led_set(0, 50, 0)
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
                    # LED: finished -> BLUE
                    self._led_set(0, 0, 50)
                    with self._cmd_lock:
                        self._action_thread = None
                        self._action_cancel = None
                        self._current_cmd = None

            # notify started and launch
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

    def _led_set(self, r: int, g: int, b: int):
        try:
            if self.arm is not None and hasattr(self.arm, "Arm_RGB_set"):
                with self._arm_io_lock:
                    self.arm.Arm_RGB_set(int(r), int(g), int(b))
        except Exception:
            pass

    def _on_error(self, ws, error):
        self.log.error("ws error: %s", error)
        self._send({"type": "error", "ts": now_iso(), "error": str(error)})

    def _on_close(self, ws, status_code, msg):
        self.log.info("ws close | code=%s msg=%s", status_code, msg)
        # Inform backend we're closing
        try:
            self._send(
                {"type": "bye", "ts": now_iso(), "code": status_code, "msg": msg}
            )
        except Exception:
            pass
        # Cleanup controllers and shared devices at app level
        # No face_follow controller anymore
        try:
            if self.face_tracking is not None:
                self.face_tracking.stop()
        except Exception:
            pass
        try:
            if self.actions is not None:
                self.actions.shutdown()  # no-op; kept for API symmetry
        except Exception:
            pass
        # App owns the shared Arm device; release safely here
        try:
            if self.arm is not None:
                for m in ("close", "shutdown", "release"):
                    if hasattr(self.arm, m):
                        try:
                            getattr(self.arm, m)()
                        except Exception:
                            pass
                        break
                self.arm = None
        except Exception:
            pass
        # Stop telemetry
        try:
            if getattr(self, "_telemetry_stop", None) is not None:
                self._telemetry_stop.set()
        except Exception:
            pass

    # ----------------- Face updates -----------------
    def _on_face_tracking_event(self, event: dict):
        # Add timestamp and forward as-is
        event = dict(event)
        event.setdefault("ts", now_iso())
        self._send(event)

    # ----------------- Transport -----------------
    def _send(self, obj: Dict[str, Any]):
        try:
            if self.ws:
                # annotate sender role for easier tracing
                payload = dict(obj)
                payload.setdefault("who", "carebot")
                self.ws.send(json.dumps(payload))
        except Exception:
            # best-effort logging; avoid raising in callback threads
            pass

    # ----------------- Telemetry: joint state streaming -----------------
    def _start_joint_stream(self, interval_ms: int = 200):
        if getattr(self, "_telemetry_thread", None):
            return
        self._telemetry_stop = threading.Event()

        def _loop():
            last = [None] * 6
            first_sent = False
            force_interval_s = 1.0
            last_force = 0.0
            while not self._telemetry_stop.is_set():
                if self.arm is not None:

                    def _read_angles(full_block: bool) -> Optional[list]:
                        try:
                            result = []
                            if full_block:
                                # Take a consistent snapshot under the lock occasionally
                                acquired = self._arm_io_lock.acquire(timeout=0.2)
                                if not acquired:
                                    return None
                                try:
                                    for i in range(6):
                                        val = self.arm.Arm_serial_servo_read(i + 1)
                                        result.append(
                                            int(val) if val is not None else None
                                        )
                                        time.sleep(0.003)
                                finally:
                                    try:
                                        self._arm_io_lock.release()
                                    except Exception:
                                        pass
                            else:
                                # Lightweight best-effort reads that yield to writers
                                for i in range(6):
                                    val = None
                                    if self._arm_io_lock.acquire(blocking=False):
                                        try:
                                            val = self.arm.Arm_serial_servo_read(i + 1)
                                        except Exception:
                                            val = None
                                        finally:
                                            try:
                                                self._arm_io_lock.release()
                                            except Exception:
                                                pass
                                    result.append(int(val) if val is not None else None)
                                    time.sleep(0.001)
                            return result
                        except Exception:
                            return None

                    now_t = time.time()
                    need_force = (not first_sent) or (
                        (now_t - last_force) >= force_interval_s
                    )
                    angles = _read_angles(full_block=need_force)
                    if angles is None:
                        # Fallback to best-effort if forced read failed to acquire
                        angles = _read_angles(full_block=False)
                    if angles is not None:
                        should_send = False
                        if not first_sent:
                            should_send = True
                        elif angles != last:
                            should_send = True
                        elif (now_t - last_force) >= force_interval_s:
                            should_send = True
                        if should_send:
                            payload = {
                                "type": "joint_state",
                                "angles": angles,
                                "ts": now_iso(),
                            }
                            self._send(payload)
                            self.log.debug("telemetry joint_state: %s", payload)
                            last = list(angles)
                            last_force = now_t
                            first_sent = True
                time.sleep(max(0.1, interval_ms / 1000.0))

        t = threading.Thread(target=_loop, name="JointTelemetry", daemon=True)
        self._telemetry_thread = t
        t.start()

    def run(self):
        while True:
            try:
                self.log.info("connecting to %s", self.ws_url)
                self.ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self.ws.run_forever(ping_interval=20, ping_timeout=10)
            except KeyboardInterrupt:
                # Graceful shutdown on Ctrl+C
                try:
                    if self.face_tracking is not None:
                        self.face_tracking.stop()
                except Exception:
                    pass
                try:
                    if self.actions is not None:
                        self.actions.shutdown()
                except Exception:
                    pass
                try:
                    if getattr(self, "_telemetry_stop", None) is not None:
                        self._telemetry_stop.set()
                except Exception:
                    pass
                try:
                    if self.arm is not None:
                        for m in ("close", "shutdown", "release"):
                            if hasattr(self.arm, m):
                                try:
                                    getattr(self.arm, m)()
                                except Exception:
                                    pass
                                break
                        self.arm = None
                except Exception:
                    pass
                break
            except Exception:
                # brief reconnect backoff
                self.log.warning("connection lost, retrying in 2s")
                time.sleep(2.0)


if __name__ == "__main__":
    base = os.path.dirname(os.path.abspath(__file__))
    app = CarebotApp(config_path=os.path.join(base, "config.json"))
    app.run()
