import os
import time
from typing import Optional
import threading

from .action_heart import ActionHeart
from .action_hug import ActionHug
from .action_init_pose import ActionInitPose


class ArmActions:
    def __init__(
        self,
        arm_device,
        arm_lock: Optional[threading.Lock] = None,
        robot_id: Optional[str] = None,
    ):
        """Wrap high-level arm actions around an already-initialized arm device.

        arm_device should be an instance compatible with Arm_Lib.Arm_Device().
        """
        if arm_device is None:
            raise RuntimeError("arm_device is required")
        self.arm = arm_device
        # Shared lock to serialize all Arm_Lib I/O (reads and writes)
        self._arm_lock = arm_lock or threading.Lock()
        # 이 인스턴스가 제어하는 로봇 ID (robot_left / robot_right)
        self.robot_id = robot_id or os.getenv("CAREBOT_ROBOT_ID")

    def set_ready_pose(self, time_ms: int = 1500):
        """Move arm to a neutral/ready pose."""
        try:
            with self._arm_lock:
                self.arm.Arm_serial_servo_write6_array(
                    [90, 150, 20, 20, 90, 30], time_ms
                )
            time.sleep(max(0.0, time_ms / 1000.0))
        except Exception:
            # If hardware is not connected, allow caller to continue; command handlers can report errors
            pass

    def shutdown(self):
        """No-op for shared arm device.

        ArmActions does not own the arm device; it's managed by the app and
        shared with other components. Avoid deleting or nulling the reference
        here to prevent interfering with other users of the same device.
        """
        pass

    def make_heart(self, cancel_event=None) -> str:
        """
        Perform a simple 'heart' gesture with the arm.
        This is adapted from do_actions notebook.
        Returns a short status string.
        """
        return ActionHeart(self.arm, robot_id=self.robot_id).run(
            cancel_event=cancel_event
        )

    def hug(self, cancel_event=None) -> str:
        """Perform a gentle 'hug' gesture with the arm and return status."""
        return ActionHug(self.arm, robot_id=self.robot_id).run(
            cancel_event=cancel_event
        )

    def init_pose(self, cancel_event=None) -> str:
        """Move the arm to a conservative initial/ready pose.

        Returns 'init_completed' or 'init_cancelled'.
        """
        return ActionInitPose(self.arm).run(cancel_event=cancel_event)

    # -------- Manual control helpers --------
    def set_joint(self, sid: int, angle: int, time_ms: int = 500) -> str:
        """Set a single joint to an angle (0-180)."""
        try:
            sid = int(sid)
            angle = max(0, min(180, int(angle)))
            t = max(0, int(time_ms))
            with self._arm_lock:
                self.arm.Arm_serial_servo_write(sid, angle, t)
            return "ok"
        except Exception as e:
            return f"error:{e}"

    def set_joints(self, angles: list, time_ms: int = 500) -> str:
        """Set all 6 joints at once. 'angles' must be length 6."""
        try:
            if not isinstance(angles, (list, tuple)) or len(angles) != 6:
                return "error:invalid_angles"
            arr = [max(0, min(180, int(a))) for a in angles]
            t = max(0, int(time_ms))
            with self._arm_lock:
                self.arm.Arm_serial_servo_write6_array(arr, t)
            return "ok"
        except Exception as e:
            return f"error:{e}"

    def nudge_joint(self, sid: int, delta: int, time_ms: int = 300) -> str:
        """Increment a joint by delta degrees (clamped to 0..180)."""
        try:
            sid = int(sid)
            d = int(delta)
            # Read current angle; if unavailable, do NOT move.
            raw = None
            try:
                with self._arm_lock:
                    raw = self.arm.Arm_serial_servo_read(sid)
            except Exception:
                return "error:read_failed"
            if raw is None:
                return "error:read_failed"
            try:
                current = int(raw)
            except Exception:
                return "error:read_failed"
            target = max(0, min(180, current + d))
            t = max(0, int(time_ms))
            with self._arm_lock:
                self.arm.Arm_serial_servo_write(sid, target, t)
            return "ok"
        except Exception as e:
            return f"error:{e}"
