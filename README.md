# Carebot 서비스

백엔드에서 오는 WebSocket 명령을 수신해 DOFBOT을 제어하는 서비스입니다.

지원 명령:

- `face_tracking` | `face_tracking_mode` | `face_tracking_모드`: 얼굴 추적 모드 시작(감지 + 팔 이동)
- `stop_face_tracking`: 얼굴 추적 모드 중지
- `make_heart`: 간단한 하트 동작 수행

## 설정

`config.json` 편집:

- `ws_url`: 백엔드 WebSocket 엔드포인트(예: `ws://127.0.0.1:8765/ws`)
- `camera_index`: OpenCV 카메라 인덱스(기본 0)
- `update_interval_ms`: 얼굴 업데이트 주기(ms)

참고: `haarcascade_frontalface_default.xml` 파일은 `Carebot` 폴더에 포함되어 있으며 자동으로 사용됩니다. 해당 파일이 없으면 OpenCV 내장 카스케이드로 대체됩니다.

환경변수 `CAREBOT_WS_URL`로 URL을 오버라이드할 수 있습니다.

## 설치 (Windows cmd)

이미 환경이 준비되어 있다면 생략 가능합니다.

```cmd
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 실행

```cmd
python app.py
```

## 메시지 형식

들어오는 메시지(예시):

- `{ "command": "face_tracking" }`
- `{ "command": "stop_face_tracking" }`
- `{ "command": "make_heart" }`

나가는 메시지(예시):

- Ack: `{ "type": "ack", "command": "face_tracking", "status": "accepted", "ts": "..." }`
- Tracking update: `{ "type": "face_tracking", "status": "running", "detected": true, "bbox": {...}, "joints": [..], "ts": "..." }`
- Result: `{ "type": "result", "command": "make_heart", "status": "completed", "outcome": "heart_completed", "ts": "..." }`
- Errors: `{ "type": "error", "error": "unknown_command", "command": "...", "ts": "..." }`
