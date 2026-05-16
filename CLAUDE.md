# 프로젝트 개요

Notion 데이터베이스를 데이터 소스로 사용하는 텔레그램 일정 알림 봇.
사용자가 노션 이름으로 등록하면, 본인이 담당/참조된 일정을 자동으로 받아볼 수 있음.

## 핵심 기능

- **평일 오전 8시 일일 발송**: 등록된 유저 전원에게 오늘 전체 일정(내 일정 + 출장/외근 + 전사 이벤트) 발송
- **3분 주기 변경 감지**: 평일 08:00~19:00 KST 동안 오늘 내 일정의 추가/변경/삭제 알림
- **5분 전 리마인더**: 시간 있는 일정은 시작 5분 전 1회성 알림
- **명령어**: `/start`, `/register`, `/today`, `/tomorrow`, `/left`, `/date`
- **유저 저장**: `users.json` 로컬 캐시 + Google Sheets 영구 백업 (재배포 후 복구용)

## 스택 (requirements.txt 고정 버전)

- `python-telegram-bot==20.7`
- `notion-client==2.2.1`
- `APScheduler==3.10.4`
- `pytz==2024.1`
- `aiohttp==3.9.3`
- `gspread==6.1.2`
- `google-auth==2.29.0`

런타임: **Python 3.11.9** (`runtime.txt`에 고정)

⚠️ 라이브러리 버전 임의 변경 금지. 특히 `python-telegram-bot` 20.x는 API 자체가 v13과 다르므로 함부로 올리면 안 됨.

## 배포 환경

- **Railway**에서 GitHub 자동 배포 (push → 자동 빌드 → 배포)
- 로컬 Python 실행 환경 없음 → **푸시 전에 import, 문법, 들여쓰기, 타입 신중히 검토 필수**
- 변경 영향 범위가 좁은 단위로 커밋해서 롤백 쉽게 유지
- Railway 빌드 로그로 배포 결과 확인, 텔레그램에서 실제 명령어로 동작 확인

## 파일 구조

```
.
├── main.py              # 봇 엔트리포인트, 핸들러 등록, 스케줄러 구동
├── notion_helper.py     # Notion API 래퍼, 일정 파싱/포맷팅
├── schedule_monitor.py  # 3분 폴링 + 5분 전 리마인더 로직
├── user_store.py        # users.json + Google Sheets 듀얼 저장소
├── requirements.txt
└── runtime.txt
```

## 모듈별 역할

### `main.py`
- `ApplicationBuilder`로 텔레그램 봇 구동 (`app.run_polling()`)
- `AsyncIOScheduler` 등록:
  - `scheduled_daily`: 매일 8시 KST, 월~금
  - `run_monitor`: 3분마다 (`minute="0,3,6,...,57"`)
- `ConversationHandler` 3개: `/start`, `/register`, `/date`
- `_validate_env()`로 시작 시 환경변수 체크 (필수/선택 분리)
- 실패 시 인라인 키보드로 "🔄 다시 시도" 콜백 제공 (`retry_daily_callback`)

### `notion_helper.py`
- 전역 `notion = AsyncClient(auth=NOTION_TOKEN)` 1개 재사용
- `_query_pages(target, long_range_days=30)`: 노션 DB에서 페이지 일괄 조회 (페이지네이션 처리, 10초 타임아웃)
- `_parse_schedule_from_pages`: 전체 일정용 파싱 (내 일정 + 출장 + 전사 이벤트 분류)
- `_filter_my_cards_from_pages`: 내 일정만 추출 (모니터링용)
- 카테고리 상수: `EXCLUDED_CATEGORIES`(휴가/공휴일 등), `TRIP_CATEGORIES`(출장/외근 등), `COMPANY_EVENT_CATEGORIES`(세미나/생일 등)
- `_is_my_card`: 담당자 컬럼 키를 여러 후보(`Assign`, `cc`, `담당자`, `Assignee`, `담당`, `할당`, `CC`, `참조`, `관련자`, `사람`)에서 탐색
- `parse_datetime_str`: ISO 문자열을 KST `datetime`으로, time 포함 여부도 함께 반환

### `schedule_monitor.py`
- 전역 상태: `_prev_state`(유저별 이전 카드 스냅샷), `_prev_state_date`(스냅샷 기준일), `_scheduler`(글로벌 참조), `_monitor_lock`(중복 폴링 방지)
- `check_and_notify`: 3분 폴링 본체. `_monitor_lock`으로 중복 실행 방지, 페이지 조회 2회 재시도
- `refresh_baseline`: `/today`, `/register`, 일일 발송 후 호출. 기준 상태 갱신 + 리마인더 일괄 등록
- `_register_reminder`: APScheduler `date` 트리거로 시작 5분 전 job 등록. job_id 패턴은 `reminder_{telegram_id}_{page_id}`
- 날짜가 바뀌면 `_prev_state` 자동 초기화
- `DEBUG_MODE=true` 환경변수로 근무시간/요일 제약 우회 가능

### `user_store.py`
- 듀얼 저장 구조: 읽기는 `users.json`(빠름), 쓰기는 둘 다 동기화
- `restore_from_sheets()`: 봇 시작 시 Sheets → `users.json` 복구 (Railway 재배포 시 데이터 보존)
- Google 인증은 `GOOGLE_CREDENTIALS`(JSON 문자열) + `GOOGLE_SHEETS_ID` 환경변수
- Sheets 연결 실패 시 로컬만으로 fallback (경고 로그)

## 환경변수

**필수**
- `TELEGRAM_TOKEN`: 텔레그램 봇 토큰
- `NOTION_TOKEN`: 노션 API 토큰
- `NOTION_DATABASE_ID`: 노션 데이터베이스 ID

**선택 (없으면 Sheets 백업 비활성화)**
- `GOOGLE_SHEETS_ID`
- `GOOGLE_CREDENTIALS`: 서비스 계정 JSON 문자열 전체

**디버그**
- `DEBUG_MODE=true`: 모니터의 근무시간/요일 제약 우회

## 코딩 규칙

### 텔레그램 메시지
- 모든 메시지는 **MarkdownV2 파싱 모드** 사용
- 동적 텍스트는 반드시 `escape_md()`로 이스케이프
- 링크 텍스트는 `escape_md_link_text()` 사용 (이스케이프 대상 문자 집합이 다름)
- 정적 문구에 들어가는 특수문자(`.`, `!`, `-`, `(`, `)` 등)도 직접 `\\` 이스케이프 필요
- 로딩 메시지는 `reply_text` → `edit_text`로 갱신 (메시지 폭주 방지)

### 시간/날짜
- 모든 시간은 **KST (`Asia/Seoul`)** 기준
- `notion_helper.py`는 `pytz.timezone("Asia/Seoul")`, `schedule_monitor.py`는 `zoneinfo.ZoneInfo("Asia/Seoul")` 사용 — 혼용 상태이므로 새 코드 추가 시 같은 파일 내 스타일 따를 것
- 카드 정렬은 `start_raw` 기준 통일 (`_sort_key` 또는 직접 `card["start_raw"]`)
- 날짜 비교는 `parse_datetime_str()` 거쳐서 `datetime` 객체로

### 비동기/동시성
- 모든 텔레그램/노션 호출은 `async`
- `_monitor_lock`은 폴링과 일일 발송이 겹치지 않게 보호
- `_user_tasks`로 유저별 진행 중인 조회 작업 추적, 새 요청 오면 기존 task 취소
- 노션 API는 `asyncio.wait_for(..., timeout=NOTION_TIMEOUT)`로 10초 타임아웃

### 에러 핸들링
- 노션 페이지 조회 실패는 단계별 재시도 (`scheduled_daily`는 3회 / `check_and_notify`는 2회 / `_send_daily_to_user`는 2회)
- 사용자에게 보이는 에러는 인라인 키보드 "🔄 다시 시도" 콜백으로 복구 경로 제공
- `try/except`에서 `Exception`을 잡을 때는 반드시 `logger.error(f"[{컨텍스트}] {e}")` 로깅 형식 유지
- 로그 prefix는 대괄호로 `[모니터]`, `[스케줄러]`, `[등록]` 등 모듈/기능 구분

### 노션 데이터 처리
- 담당자 컬럼 키가 워크스페이스마다 다를 수 있으므로 `_is_my_card` / `_get_assignees`의 키 후보 리스트를 통해 탐색
- 새 카테고리 추가 시 `EXCLUDED_CATEGORIES`, `TRIP_CATEGORIES`, `COMPANY_EVENT_CATEGORIES` 상수에 등록
- `is_card_on_date`로 멀티데이 일정의 해당 날짜 포함 여부 판단

## 커밋 규칙

- **Conventional Commits**: `feat:`, `fix:`, `refactor:`, `chore:`, `docs:`, `test:`, `perf:`
- 메시지는 **한글**로 작성
- 한 커밋엔 한 가지 논리적 변경만
- 변경 파일 많으면 논리 단위로 나눠서 여러 커밋으로 분리
- 예시: `fix: 5분 전 리마인더 날짜 바뀐 후 중복 등록 버그 수정`

## 작업 시작/종료 루틴

**작업 시작 시 (사무실/집 두 PC 환경 때문에 필수):**
1. `git pull`로 다른 PC 변경사항 먼저 가져오기
2. `git status`로 깨끗한 상태 확인 후 작업 시작

**작업 종료 시:**
1. 변경사항 검토 (`git diff`)
2. 푸시 전 정합성 체크: import 누락, 들여쓰기, 타입 힌트, MarkdownV2 이스케이프
3. 논리 단위로 commit, 마지막에 push
4. Railway 배포 로그 확인 → 텔레그램에서 실제 동작 확인

## 절대 금지

- `.env`, `users.json`, 서비스 계정 JSON 파일 커밋 금지 (`.gitignore` 확인 필수)
- 토큰/키 하드코딩 금지 (반드시 `os.environ` 사용)
- `python-telegram-bot` 메이저 버전 변경 금지 (API breaking change)
- `notion`, `_scheduler` 같은 모듈 전역 객체를 새로 만들지 말고 기존 것 재사용
- 텔레그램 메시지에 escape 없이 사용자 입력/노션 데이터 직접 삽입 금지
