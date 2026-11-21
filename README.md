# 한국 바이럴 콘텐츠 모니터

유튜브 · X · 인스타그램 · 틱톡 · 스레드에서 **좋아요 1,000개 이상**인 한국 콘텐츠를 매일 아침 모아
리포트(HTML/CSV)로 만들고 텔레그램으로 알려줍니다.

## 작동 방식

| 플랫폼 | 수집 방법 | 비용 |
|---|---|---|
| 유튜브 | 공식 API: 한국 인기 급상승 200개 + 한국 지역 최근 조회수순 검색 | 무료 (하루 약 200유닛 사용 / 한도 10,000) |
| X | Apify 수집기, `lang:ko min_faves:1000` 검색으로 조건 맞는 글만 수집 | 소액 (수집 건수당 과금) |
| 인스타그램 | Apify, 한국어 인기 해시태그의 게시물 | 소액 |
| 틱톡 | Apify, 한국어 해시태그의 영상 | 소액 |
| 스레드 | Apify, 한국어 키워드 인기 검색 | 소액 |

- 한글이 없는 콘텐츠는 걸러냅니다 (`korean_only`).
- 최근 2일 안에 올라온 것만 봅니다 (`lookback_days`).
- 전날까지 본 적 없는 콘텐츠에는 **NEW** 표시가 붙습니다.
- 한 플랫폼이 실패해도 나머지는 정상 수집됩니다.

## 설치 (15분)

### 1. 키 발급
1. **유튜브 API 키** (무료): [Google Cloud Console](https://console.cloud.google.com/) → 새 프로젝트 → "YouTube Data API v3" 사용 설정 → 사용자 인증 정보 → API 키 만들기
2. **Apify 토큰**: [apify.com](https://apify.com) 가입 → Settings → API & Integrations → Personal API token
   무료 플랜에 매달 사용 크레딧이 들어 있어 처음엔 그 안에서 테스트할 수 있습니다.
3. **텔레그램 알림** (선택, 무료): 텔레그램에서 `@BotFather` → `/newbot` → 토큰 받기.
   만든 봇에게 아무 메시지나 보낸 뒤 `https://api.telegram.org/bot<토큰>/getUpdates` 를 열어 `chat.id` 확인.

### 2. GitHub에 올리기
1. GitHub에서 새 저장소(Private 추천)를 만들고 이 폴더 내용을 전부 올립니다.
2. 저장소 → Settings → Secrets and variables → Actions → **New repository secret** 으로 추가:
   - `YOUTUBE_API_KEY`
   - `APIFY_TOKEN`
   - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (선택)
   - `X_BEARER_TOKEN` (X 공식 API를 쓸 때만)
3. Actions 탭 → `daily-viral-monitor` → **Run workflow** 로 첫 실행을 해 봅니다.

이후 매일 **오전 8시 7분**에 자동 실행되고, 결과는 저장소의 `reports/` 폴더에 날짜별로 쌓입니다.
(`reports/index.html` 이 항상 최신본)

> 리포트를 웹페이지로 보고 싶으면: Settings → Pages → Branch `main` / 폴더 `/(root)` 선택 →
> `https://<아이디>.github.io/<저장소>/reports/` 에서 열립니다. (Private 저장소의 Pages는 유료 플랜 필요)

### 내 컴퓨터에서 실행
```bash
pip install -r requirements.txt
python monitor.py --demo          # 키 없이 샘플 리포트 미리보기
export YOUTUBE_API_KEY=... APIFY_TOKEN=...
python monitor.py                 # 실제 수집
```

## 비용 줄이는 법
- 처음 1주일은 `config.yaml` 의 수집량(`results_per_hashtag`, `max_items`, `max_posts_per_keyword`)을 그대로 두고
  Apify 콘솔 → Usage 에서 하루 비용을 확인한 뒤 조정하세요.
- 비용을 0원에 가깝게: 유튜브만 켜고 나머지는 `enabled: false`.
- 비싸게 나오는 플랫폼은 해시태그/키워드 수를 줄이는 게 가장 효과적입니다.

## 설정 바꾸기 (`config.yaml`)
- `min_likes`: 기준 좋아요 수
- 해시태그/키워드: 관심 분야에 맞게 바꾸면 더 정확해집니다.
- Apify 수집기(`apify_actor`)는 다른 것으로 바꿔도 됩니다. 결과 필드 이름이 달라도 대부분 자동 인식합니다.
  입력 형식이 다른 수집기라면 `monitor.py` 의 해당 `fetch_...` 함수에서 입력값만 맞춰 주면 됩니다.

## 알아둘 점
- "플랫폼 전체의 모든 바이럴"을 빠짐없이 잡지는 못합니다. 유튜브는 거의 완전하지만, 나머지는
  지정한 해시태그·키워드 범위 안의 인기 콘텐츠입니다.
- Apify 같은 서드파티 수집은 각 플랫폼 이용약관상 회색지대이고, 플랫폼 변경으로 수집기가
  일시적으로 안 될 수 있습니다. 리포트 상단에 실패한 플랫폼이 표시됩니다.
