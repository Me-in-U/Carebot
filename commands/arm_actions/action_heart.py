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
            # 사용자 제공 왼팔 하트 동작 3단계 각도(순서대로 수행)
            # S1..S6: [0,115,40,20,90,0] -> [0,50,55,20,90,0] -> [0,50,25,0,90,0]
            left_poses = [
                [0, 115, 40, 20, 90, 0],
                [0, 50, 55, 20, 90, 0],
                [0, 50, 25, 0, 90, 0],
            ]

            def mirror_for_right(p: list) -> list:
                # 기존 구현 관찰에 따르면 S1(베이스), S5(손목 yaw)만 좌우 미러링(180 - v)
                q = list(p)
                try:
                    q[0] = max(0, min(180, 180 - int(q[0])))
                    q[4] = max(0, min(180, 180 - int(q[4])))
                except Exception:
                    pass
                return q

            if self.robot_id == "robot_right":
                poses = [mirror_for_right(p) for p in left_poses]
            else:
                # robot_left 또는 기본
                poses = left_poses

            neutral = [90, 150, 20, 20, 90, 30]
            time_ms = 1200

            # 단계별 수행
            for i, pose in enumerate(poses):
                self._write6_reliable(pose, time_ms)
                if not self._sleep_interruptible(time_ms / 1000.0, cancel_event):
                    return "heart_cancelled"
                # 단계 사이 짧은 유지
                if i < len(poses) - 1:
                    if not self._sleep_interruptible(0.3, cancel_event):
                        return "heart_cancelled"

            # 마지막 잠깐 유지
            if not self._sleep_interruptible(0.4, cancel_event):
                return "heart_cancelled"

            # 복귀: 뉴트럴 포즈
            self._write6_reliable(neutral, time_ms)
            if not self._sleep_interruptible(time_ms / 1000.0, cancel_event):
                return "heart_cancelled"
        except Exception:
            # 하드웨어가 없더라도 상위 흐름을 막지 않음
            pass

        return "heart_completed"
