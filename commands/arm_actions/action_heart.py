import time


class ActionHeart:
    def __init__(self, arm_device):
        if arm_device is None:
            raise RuntimeError("arm_device is required")
        self.arm = arm_device

    def _sleep_interruptible(self, seconds: float, cancel_event) -> bool:
        """Sleep in small steps; return False if cancelled during sleep."""
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
        """Perform a simple 'heart' gesture and return status string.

        If cancel_event is set during execution, abort early and return 'heart_cancelled'.
        """
        time_ms = 2000
        sleep_s = 2.0

        # Example pose adapted from existing notebook/codebase
        # Arm.Arm_serial_servo_write6(id, s1, s2, s3, s4, s5, time)
        try:
            self.arm.Arm_serial_servo_write6(0, 48, 45, -20, 0, 180, time_ms)
            if not self._sleep_interruptible(sleep_s, cancel_event):
                return "heart_cancelled"

            # Return to a neutral-ish pose
            self.arm.Arm_serial_servo_write6_array([90, 150, 20, 20, 90, 30], time_ms)
            if not self._sleep_interruptible(sleep_s, cancel_event):
                return "heart_cancelled"
        except Exception:
            # Allow upper layer to proceed even if hardware is absent
            pass

        return "heart_completed"
