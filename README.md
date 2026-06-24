# 🎮 SB.GG — 차세대 LoL 전적 분석 플랫폼

<div align="center">

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-2.x-000000?style=for-the-badge&logo=flask&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-Cached-003B57?style=for-the-badge&logo=sqlite&logoColor=white)
![Chart.js](https://img.shields.io/badge/Chart.js-Radar-FF6384?style=for-the-badge&logo=chartdotjs&logoColor=white)
![Riot API](https://img.shields.io/badge/Riot%20Games-API-D32936?style=for-the-badge)

**단순 KDA를 넘어, 당신의 플레이를 6개의 축으로 해부합니다.**

</div>

---

## 🔥 프로젝트 소개

SB.GG는 Riot Games API를 활용한 **League of Legends 전적 분석 웹 플랫폼**입니다.

기존 전적 검색 사이트가 KDA와 승률에만 집중하는 것과 달리, SB.GG는 플레이어의 데이터를 **6개 핵심 축**으로 재가공하여 "내가 어떤 스타일의 플레이어인가"를 직관적으로 시각화합니다.

---

## ✨ 주요 기능

### 🕸️ 헥사 레이더 차트 (Hexa Radar Chart)
플레이어의 최근 20게임 데이터를 6개 축으로 집계하여 Chart.js 레이더 차트로 시각화합니다.

| 축 | 측정 지표 |
|---|---|
| **전투력** | (킬 + 어시스트) 평균 |
| **성장력** | 평균 획득 골드 |
| **시야 장악** | 평균 시야 점수 |
| **생존력** | `max(0, 20 - 데스)` 기반 역산 |
| **오브젝트 기여** | 오브젝트 데미지 |
| **합류 속도** | 킬 관여율 (KP%) |

### 🏷️ 데이터 기반 딥 태그
레이더 차트 분석 결과를 바탕으로 조건부 성향 태그를 자동 부여합니다.

- **성향 태그**: `⚔️ 피도 눈물도 없는 전투광`, `💰 압도적 성장형 캐리`, `🛡️ 강철의 불사신` 등
- **경고 태그**: `🚨 갱킹 주의! 시야 점수 심각`, `🚨 팀원들이 고통받는 고립형` 등

### 📊 게임별 성적 등급 시스템
KDA, KP%, CS/min, 승패를 종합 점수화하여 **S+ ~ C** 7단계 등급 부여

### 🔥 팀 기여도 자동 판정
같은 팀 내 OP 스코어 비교를 통해 `캐리 🔥 / 억까 😭 / 버스 🚌 / 1인분 ⚖️` 자동 판정

### 🏆 기타 기능
- **챌린저 리더보드**: 한국 서버 챌린저 Top 50 실시간 조회
- **관전 (Spectate)**: 챌린저 선수의 현재 진행 중인 게임 표시
- **메타 분석**: 패치별 포지션별 챔피언 티어표
- **멀티 서치**: 최대 5명 동시 비교
- **챔피언 추천**: 플레이 성향 기반 챔피언 추천

---

## 🛠️ 기술 스택

```
Backend   : Python 3.10+, Flask 2.x
Database  : SQLite (5분 단위 API 캐싱)
Frontend  : Vanilla HTML/CSS (글래스모피즘 다크 테마), JavaScript
Charting  : Chart.js (Radar Chart)
Fonts     : Pretendard, Montserrat (Google Fonts)
API       : Riot Games API v5
```

---

## 🏗️ 아키텍처

```
┌─────────────────────────────────────────┐
│              Browser (Client)            │
│    HTML/CSS/JS + Chart.js Radar Chart    │
└───────────────────┬─────────────────────┘
                    │ HTTP
┌───────────────────▼─────────────────────┐
│            Flask Application            │
│  ┌──────────┐  ┌──────────────────────┐ │
│  │ Routing  │  │   Business Logic     │ │
│  │  /search │  │  calc_radar()        │ │
│  │  /meta   │  │  generate_deep_tags()│ │
│  │  /leader │  │  calc_game_grade()   │ │
│  └──────────┘  └──────────────────────┘ │
│  ┌──────────────────────────────────┐   │
│  │       SQLite Cache Layer         │   │
│  │  db_read() / db_write()          │   │
│  │  TTL: 검색 5분 / 리더보드 10분   │   │
│  └──────────────────────────────────┘   │
└───────────────────┬─────────────────────┘
                    │ HTTPS (Rate-Limited)
┌───────────────────▼─────────────────────┐
│          Riot Games API v5               │
│  Account v1 / Match v5 / League v4      │
│  Spectator v5 / Champion Mastery v4     │
└─────────────────────────────────────────┘
```

---

## ⚡ 빠른 시작

### 1. 저장소 클론
```bash
git clone https://github.com/YOUR_USERNAME/sbgg.git
cd sbgg
```

### 2. 가상환경 설정 및 패키지 설치
```bash
python -m venv venv
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
```

### 3. API 키 설정
`.env.example`을 복사하여 `.env` 파일 생성 후 키 입력:
```bash
cp .env.example .env
```
```env
RIOT_API_KEY=RGAPI-your-key-here
```
> API 키 발급: https://developer.riotgames.com/

### 4. 서버 실행
```bash
python app.py
```
`http://127.0.0.1:5000` 접속

---

## 🚀 배포 (PythonAnywhere)

1. https://www.pythonanywhere.com/ 회원가입 (무료)
2. Files 탭에서 프로젝트 업로드 또는 `git clone`
3. Bash 콘솔에서: `pip install --user flask requests python-dotenv`
4. Web 탭 → Add new web app → Manual config → Python 3.10
5. WSGI 파일에 추가:
   ```python
   import sys
   sys.path.insert(0, '/home/YOUR_USERNAME/sbgg')
   from app import app as application
   ```
6. 환경변수 탭에서 `RIOT_API_KEY` 설정 후 Reload

---

## 📁 프로젝트 구조

```
sbgg/
├── app.py              # Flask 서버, 라우팅, 비즈니스 로직
├── champion_meta.py    # 챔피언 메타 데이터 (티어, 역할, 난이도)
├── requirements.txt    # Python 패키지 목록
├── .env.example        # 환경 변수 템플릿
├── .gitignore          # Git 제외 목록
└── templates/
    └── index.html      # 전체 프론트엔드 (Jinja2 템플릿)
```

---

## 🗺️ 로드맵

- [x] Phase 1: 로컬 환경 기반 아키텍처, API 연동, UI/UX, 캐싱 시스템
- [ ] Phase 2: PythonAnywhere 배포, GitHub 포트폴리오화
- [ ] Phase 3: 반응형 모바일 UI, 추가 분석 알고리즘
- [ ] Phase 4: Production API 키 신청, 커스텀 도메인 연결

---

## ⚠️ 주의사항

- 현재 **Personal API Key** 사용 중 (Rate Limit: 20 req/s, 100 req/2min)
- SQLite 캐싱으로 불필요한 API 호출 최소화
- Personal Key는 24시간마다 갱신 필요 → `.env` 파일 업데이트

---

## 📄 라이선스

MIT License — Made with ❤️ and LoL data
