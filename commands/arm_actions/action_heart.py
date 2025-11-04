import time
import threading
from typing import Optional


class ActionHeart:
    def __init__(
        self,
        arm_device,
        robot_id: Optional[str] = None,
        arm_lock: Optional[threading.Lock] = None,
    ):
        if arm_device is None:
            raise RuntimeError("arm_device is required")
        self.arm = arm_device
        # 로봇 구분 (robot_left / robot_right). 기본값은 None -> 공통 동작
        self.robot_id = robot_id
        # Arm_Lib I/O 직렬화를 위한 잠금 (텔레메트리/다른 쓰레드와 경합 방지)
        self._arm_lock = arm_lock or threading.Lock()

    def _write6_reliable(self, angles: list, time_ms: int):
        """write6_array 신뢰성 향상: 1차 전송 후 짧은 지연, 빠르게 잠금 가능하면
        동일 명령을 한 번 더 전송(간헐적 드랍 보완)."""
        try:
            with self._arm_lock:
                self.arm.Arm_serial_servo_write6_array(angles, int(time_ms))
        except Exception:
            pass
        try:
            time.sleep(0.03)
        except Exception:
            pass
        try:
            if self._arm_lock.acquire(timeout=0.05):
                try:
                    self.arm.Arm_serial_servo_write6_array(angles, int(time_ms))
                finally:
                    try:
                        self._arm_lock.release()
                    except Exception:
                        pass
        except Exception:
            pass

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
        """'하트' 제스처 수행 (로봇 좌/우에 따라 약간 다르게 동작) 후 상태 문자열 반환.

        - robot_left: 왼팔 기준의 미러 포즈
        - robot_right: 오른팔 기준의 미러 포즈
        - 그 외/미지정: 공통 기본 포즈

        cancel_event 가 설정되면 즉시 중단하고 'heart_cancelled' 반환.
        """
        try:
            # 안전한 기본 시간/대기
            time_ms = 1400

            # 좌/우 로봇별로 미세하게 다른 포즈를 사용 (하드웨어에 맞춰 조정 가능)
            if self.robot_id == "robot_left":
                # 왼쪽 로봇: 손목(5번) 각도를 왼쪽 방향으로, 약간 낮게
                pose1 = [85, 135, 30, 30, 60, 35]
                pose2 = [85, 150, 38, 38, 70, 40]
            elif self.robot_id == "robot_right":
                # 오른쪽 로봇: 손목(5번) 각도를 오른쪽 방향으로, 약간 낮게
                pose1 = [95, 135, 30, 30, 120, 35]
                pose2 = [95, 150, 38, 38, 110, 40]
            else:
                # 기본(공통) 포즈
                pose1 = [90, 140, 32, 32, 90, 35]
                pose2 = [90, 150, 38, 38, 90, 40]

            neutral = [90, 150, 20, 20, 90, 30]

            # 단계 1: 포즈1
            self._write6_reliable(pose1, time_ms)
            if not self._sleep_interruptible(time_ms / 1000.0, cancel_event):
                return "heart_cancelled"

            # 짧은 유지
            if not self._sleep_interruptible(0.4, cancel_event):
                return "heart_cancelled"

            # 단계 2: 포즈2 (하트 마무리)
            self._write6_reliable(pose2, time_ms)
            if not self._sleep_interruptible(time_ms / 1000.0, cancel_event):
                return "heart_cancelled"

            # 잠깐 유지
            if not self._sleep_interruptible(0.6, cancel_event):
                return "heart_cancelled"

            # 복귀: 뉴트럴 포즈
            self._write6_reliable(neutral, time_ms)
            if not self._sleep_interruptible(time_ms / 1000.0, cancel_event):
                return "heart_cancelled"
        except Exception:
            # 하드웨어가 없더라도 상위 흐름을 막지 않음
            pass

        return "heart_completed"
