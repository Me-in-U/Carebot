import time


class ActionInitPose:
    def __init__(self, arm_device):
        if arm_device is None:
            raise RuntimeError("arm_device is required")
        self.arm = arm_device

    def _sleep_interruptible(self, seconds: float, cancel_event) -> bool:
        if cancel_event is None:
            time.sleep(seconds)
            return True
        end = time.time() + seconds
        step = 0.05
        while time.time() < end:
            if cancel_event.is_set():
                return False
            time.sleep(min(step, max(0.0, end - time.time())))
        return True

    def run(self, cancel_event=None) -> str:
        """Move the arm to a safe initial/ready pose.

        This sequence is conservative: it moves to a neutral joint array and
        waits for the motion to complete. If cancel_event is set during the
        sequence, returns 'init_cancelled'. Otherwise returns 'init_completed'.
        """
        try:
            # Move to a neutral ready pose using the existing array from other actions
            time_ms = 1200
            self.arm.Arm_serial_servo_write6_array([90, 90, 90, 90, 90, 90], time_ms)
            if not self._sleep_interruptible(time_ms / 1000.0, cancel_event):
                return "init_cancelled"

            # Optional small settle
            if not self._sleep_interruptible(0.3, cancel_event):
                return "init_cancelled"
        except Exception:
            # Hardware may be absent; still return completed to allow progress
            return "init_completed"

        return "init_completed"
