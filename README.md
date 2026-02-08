# 42LogTime

42 학습시간 간편 조회 서비스

42 Intra API를 이용해 사용자의 학습 시간을 월 단위로 집계·조회할 수 있는 웹 서비스입니다.
OAuth 로그인 후, 당월 학습 시간과 일자별 학습 기록을 간편하게 확인할 수 있습니다.

---

## Screenshots

### 당월 누적 학습시간 현황 조회
<img width="370" height="114" alt="image" src="https://github.com/user-attachments/assets/7066f7b2-e41d-4c86-a674-81b2ce5c5204" /><br/>
<img width="370" height="138" alt="image" src="https://github.com/user-attachments/assets/2a6dbdca-8b52-4701-9d39-89110d712e70" />

### 당일 누적 학습시간 현황 조회
<img width="370" height="131" alt="image" src="https://github.com/user-attachments/assets/362634ed-c93f-4464-8f0e-1a55086d3d04" />

### 당월 일별 학습시간 로그 조회
<img width="370" height="415" alt="image" src="https://github.com/user-attachments/assets/90094e11-79f6-4589-88b3-757c6c6c5612" />



## Features

- 42 Intra OAuth 로그인
- 당월 학습 시간 자동 집계
- 일자별 학습 시간 조회
- Asia/Seoul 타임존 기준 계산
- FastAPI 기반 REST API
- SPA(React 등) 연동용 API 제공

---

## Tech Stack

- **Frontend**
  - React (via CDN, no build step)
  - HTML / CSS / JavaScript
  - TailwindCSS (via CDN)

- **Backend**
  - Python 3.12.3
  - FastAPI
  - Uvicorn
  - Requests

- **External API**
  - 42 Intra API

---

## Requirements

- Python 3.12.3
- Python packages:
  ```txt
  fastapi==0.128.1
  uvicorn==0.40.0
  requests==2.32.5
  ```

---

## Installation

### 1. Clone repository
```bash
git clone https://github.com/your-username/42LogTime.git
cd 42LogTime
```

### 2. Create virtual environment
```bash
python -m venv venv
source venv/bin/activate
```

### 3. Install dependencies
```bash
pip install -r requirements.txt
```

---

## Environment Variables

보안을 위해 환경변수 사용을 권장합니다.

```bash
export FT_CLIENT_ID="your_42_client_id"
export FT_CLIENT_SECRET="your_42_client_secret"
export APP_BASE_URL="http://localhost:8000"
export FT_REDIRECT_URI="http://localhost:8000/callback"
```

---

## Run Server

```bash
uvicorn server:app --host 0.0.0.0 --port 8000
```

접속:
```
http://localhost:8000
```

---

## API Overview

### GET /
- 메인 엔트리
- 인증 여부에 따라 `/time` 또는 OAuth 로그인 페이지로 이동

### GET /callback
- 42 OAuth 콜백 엔드포인트

### GET /time
- React SPA HTML 반환

### GET /api/time
- 월별 학습 시간 데이터(JSON)
- 인증 필요

### POST /api/state
- 클라이언트에서 감지한 상태 정보 저장
- (모니터 상태, 잠금 상태 등)

---

## Notes

- 학습 시간 계산은 42 Intra locations API 기반입니다.
- 월별 데이터는 현재 날짜 기준으로 집계됩니다.
- 사용자 상태 정보는 서버 메모리에 임시 저장됩니다.
  (프로덕션 환경에서는 DB 연동을 권장합니다.)

---

## License

MIT License  
© 2026 lh7721004
