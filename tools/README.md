# Carebot 테스트 서버 & 프론트엔드

이 폴더에는 Carebot 앱을 테스트하기 위한 최소한의 WebSocket 백엔드와 간단한 프론트엔드 페이지가 들어 있습니다.

## 기능

- 백엔드(`backend_server.py`)는 `ws://0.0.0.0:8765/ws`에서 대기합니다.
  - 프론트엔드에서 전송한 명령(예: `make_heart`, `hug`, `face_tracking`)을 수신합니다.
  - 수신한 명령을 연결된 모든 Carebot 클라이언트로 중계합니다.
  - Carebot 앱에서 보내는 이벤트(진행/결과/face_tracking 업데이트)를 모든 프론트엔드로 중계합니다.
- 프론트엔드(`frontend.html`)는 백엔드에 연결하고 버튼으로 명령을 보낼 수 있습니다.
  - 백엔드를 통해 Carebot 앱에서 수신한 모든 메시지를 로그로 보여줍니다.

## 요구 사항

- Python 3.9+
- 의존성 설치:

```bash
pip install -r requirements.txt
```

## 실행 방법

1. 백엔드 실행:

```bash
python backend_server.py
```

2. 프론트엔드 열기:

- `frontend.html`을 더블클릭하여 브라우저에서 열거나,
- 최신 브라우저(Chrome/Edge/Firefox)에서 `file:///.../frontend.html` 경로로 직접 엽니다.

3. Carebot 앱 실행(다른 터미널에서):

- `Carebot/config.json`의 `ws_url`이 `ws://127.0.0.1:8765/ws`를 가리키는지 확인합니다.
- 앱을 실행해 백엔드에 연결되도록 합니다.

4. 프론트엔드에서:

- Connect를 누른 뒤, Start Tracking / Stop Tracking / Make Heart / Hug 버튼으로 명령을 전송합니다.
- Carebot 앱의 진행/결과 로그가 실시간으로 표시됩니다.

## 참고

- 이 백엔드는 테스트용 허브로, 인증이나 영속성 기능은 없습니다.
- 프론트엔드를 여러 개 띄워도 모두 동일한 이벤트 스트림을 수신합니다.
- Linux에서 카메라가 여러 개인 경우 `Carebot/config.json`의 `camera_index` 값을 조정하세요.
