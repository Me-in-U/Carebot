import time
import threading
from typing import Optional, Dict, Any


class ActionHug:
    def __init__(
        self,
        arm_device,
        robot_id: Optional[str] = None,
        arm_lock: Optional[threading.Lock] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        if arm_device is None:
            raise RuntimeError("arm_device is required")
        self.arm = arm_device
        self.robot_id = robot_id
        # Arm_Lib I/O 직렬화를 위한 잠금 (텔레메트리/다른 쓰레드와 경합 방지)
        self._arm_lock = arm_lock or threading.Lock()
        # 구성 전달(타이밍 등) - 없으면 기본값 사용
        self._config = config or {}

    def _write6_reliable(self, angles: list, time_ms: int):
        """write6_array를 신뢰성 있게 전송: 1차 전송 후 아주 짧은 지연 뒤
        잠금이 바로 가능하면 동일 명령을 한 번 더 전송(경우에 따라 첫 전송이
        드랍되는 상황을 보완)."""
        try:
            with self._arm_lock:
                self.arm.Arm_serial_servo_write6_array(angles, int(time_ms))
        except Exception:
            # 1차 전송 에러는 조용히 무시하고 아래 재시도에 기대
            pass
        # 짧은 가드 지연 (시리얼 버스 정리)
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
        """포옹 동작: 왼팔 기준 시퀀스에 맞춰 수행하고, 오른팔은 좌우 미러로 수행.

        시퀀스(왼팔 기준):
          1) 팔 벌리기: [90,90,85,65,90,30]
          2) 끌어안기1: [90,85,65,65,90,30]
          3) 끌어안기2: [90,65,60,80,90,30]
          4) 토닥토닥 x2 반복: [90,65,60,55,90,120] <-> [90,65,60,70,90,30]
          5) 뉴트럴 복귀
        """
        try:
            neutral = [90, 150, 20, 20, 90, 30]

            # 왼팔 기준 포즈들
            left_open = [90, 90, 85, 65, 90, 30]
            left_close1 = [90, 85, 65, 65, 90, 30]
            left_close2 = [90, 65, 60, 80, 90, 30]
            left_pat_a = [90, 65, 60, 55, 90, 120]
            left_pat_b = [90, 65, 60, 70, 90, 30]

            def mirror_for_right(p: list) -> list:
                # 좌우 미러: S1(베이스), S5(손목 yaw)만 180 - v (90은 그대로)
                q = list(p)
                try:
                    q[0] = max(0, min(180, 180 - int(q[0])))
                    q[4] = max(0, min(180, 180 - int(q[4])))
                except Exception:
                    pass
                return q

            if self.robot_id == "robot_right":
                open_pose = mirror_for_right(left_open)
                close1 = mirror_for_right(left_close1)
                close2 = mirror_for_right(left_close2)
                pat_a = mirror_for_right(left_pat_a)
                pat_b = mirror_for_right(left_pat_b)
            else:
                open_pose = left_open
                close1 = left_close1
                close2 = left_close2
                pat_a = left_pat_a
                pat_b = left_pat_b

            # 타이밍 (config 기반, 없으면 기본)
            move_ms = int(self._config.get("hug_move_ms", 1100))
            hold_between_s = float(self._config.get("hug_hold_between_s", 0.25))
            pat_ms = int(self._config.get("hug_pat_ms", 450))
            pat_hold_s = float(self._config.get("hug_pat_hold_s", 0.15))
            pat_repeat = int(self._config.get("hug_pat_repeat", 2))
            back_ms = int(self._config.get("hug_back_ms", 1200))

            # 1) 팔 벌리기
            self._write6_reliable(open_pose, move_ms)
            if not self._sleep_interruptible(max(0.0, move_ms / 1000.0), cancel_event):
                return "hug_cancelled"

            # 2) 끌어안기 (2단계)
            self._write6_reliable(close1, move_ms)
            if not self._sleep_interruptible(max(0.0, move_ms / 1000.0), cancel_event):
                return "hug_cancelled"
            if not self._sleep_interruptible(hold_between_s, cancel_event):
                return "hug_cancelled"
            self._write6_reliable(close2, move_ms)
            if not self._sleep_interruptible(max(0.0, move_ms / 1000.0), cancel_event):
                return "hug_cancelled"

            # 3) 토닥토닥 2회 반복
            for _ in range(max(1, pat_repeat)):
                self._write6_reliable(pat_a, pat_ms)
                if not self._sleep_interruptible(
                    max(0.0, pat_ms / 1000.0), cancel_event
                ):
                    return "hug_cancelled"
                if not self._sleep_interruptible(pat_hold_s, cancel_event):
                    return "hug_cancelled"
                self._write6_reliable(pat_b, pat_ms)
                if not self._sleep_interruptible(
                    max(0.0, pat_ms / 1000.0), cancel_event
                ):
                    return "hug_cancelled"
                if not self._sleep_interruptible(pat_hold_s, cancel_event):
                    return "hug_cancelled"

            # 4) 복귀(뉴트럴)
            self._write6_reliable(neutral, back_ms)
            if not self._sleep_interruptible(max(0.0, back_ms / 1000.0), cancel_event):
                return "hug_cancelled"
        except Exception:
            pass

        return "hug_completed"
