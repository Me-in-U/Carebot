import time


class ActionHug:
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
        """Perform a gentle 'hug' like gesture and return status string.

        Note: This is a conservative example sequence. Adjust joint values to match your hardware's safe range.
        """
        try:
            # Open arms slightly (spread) then bring forward as if hugging
            self.arm.Arm_serial_servo_write6_array([90, 120, 20, 20, 70, 20], 1200)
            if not self._sleep_interruptible(1.2, cancel_event):
                return "hug_cancelled"

            self.arm.Arm_serial_servo_write6_array([90, 160, 35, 35, 100, 40], 1500)
            if not self._sleep_interruptible(1.5, cancel_event):
                return "hug_cancelled"

            # Hold the pose briefly
            if not self._sleep_interruptible(0.8, cancel_event):
                return "hug_cancelled"

            # Return to neutral
            self.arm.Arm_serial_servo_write6_array([90, 150, 20, 20, 90, 30], 1200)
            if not self._sleep_interruptible(1.2, cancel_event):
                return "hug_cancelled"
        except Exception:
            pass

        return "hug_completed"
