import time
import threading
from typing import Optional


class ActionHug:
    def __init__(
        self,
        arm_device,
        robot_id: Optional[str] = None,
        arm_lock: Optional[threading.Lock] = None,
    ):
        if arm_device is None:
            raise RuntimeError("arm_device is required")
        self.arm = arm_device
        self.robot_id = robot_id
        # Arm_Lib I/O 직렬화를 위한 잠금 (텔레메트리/다른 쓰레드와 경합 방지)
        self._arm_lock = arm_lock or threading.Lock()

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
        """부드러운 '포옹' 제스처 수행 후 상태 문자열 반환 (좌/우 로봇에 맞게 미러링).

        하드웨어 안전 범위에 맞춰 각도를 조정해 사용하세요.
        """
        try:
            neutral = [90, 150, 20, 20, 90, 30]

            # 좌/우 로봇별 팔 벌리기/모으기에서 손목/팔각 미세 차등
            if self.robot_id == "robot_left":
                open_pose = [90, 120, 20, 20, 70, 20]
                close_pose = [90, 160, 35, 35, 95, 40]
            elif self.robot_id == "robot_right":
                open_pose = [90, 120, 20, 20, 110, 20]
                close_pose = [90, 160, 35, 35, 105, 40]
            else:
                open_pose = [90, 120, 20, 20, 90, 20]
                close_pose = [90, 160, 35, 35, 100, 40]

            # 펼치기
            try:
                with self._arm_lock:
                    self.arm.Arm_serial_servo_write6_array(open_pose, 1200)
            except Exception:
                pass
            if not self._sleep_interruptible(1.2, cancel_event):
                return "hug_cancelled"

            # 끌어안기
            try:
                with self._arm_lock:
                    self.arm.Arm_serial_servo_write6_array(close_pose, 1500)
            except Exception:
                pass
            if not self._sleep_interruptible(1.5, cancel_event):
                return "hug_cancelled"

            # 잠시 유지
            if not self._sleep_interruptible(0.8, cancel_event):
                return "hug_cancelled"

            # 복귀
            try:
                with self._arm_lock:
                    self.arm.Arm_serial_servo_write6_array(neutral, 1200)
            except Exception:
                pass
            if not self._sleep_interruptible(1.2, cancel_event):
                return "hug_cancelled"
        except Exception:
            pass

        return "hug_completed"
