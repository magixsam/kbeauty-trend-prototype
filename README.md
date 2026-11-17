# SEOUL SCAN — K-뷰티 트렌드 미디어 프로토타입

미국(영어권) 20~30대를 타겟으로 한 **K-뷰티 화장품 트렌드 웹매거진 프로토타입**입니다.
모든 아티클은 **실제 한국 유튜브 영상 1편 + 그 영상의 실제 한국어 댓글**에서 출발합니다
(가짜 URL·지어낸 댓글 없음).

바로 열어보기: **`index.html`** — 데이터가 파일 안에 들어 있어
서버 없이 폰/PC 브라우저에서 열면 됩니다.

---

## 1. 폴더 구성

```
kbeauty-trend-prototype/
├── index.html                      ← GitHub Pages가 서비스하는 라이브 페이지(단일 파일 빌드 결과)
├── index-fetch.html                ← 개발용 원본 템플릿(같은 폴더의 articles.json을 fetch)
├── articles.json                   ← 매거진 데이터(아티클 4편)
├── summarize_kbeauty.py            ← 수집·요약 파이프라인 (표준 라이브러리만)
├── build_preview.py                ← index-fetch.html + articles.json → index.html 단일 파일 빌드
├── config/videos.json              ← 수집 대상 영상 목록(카테고리·메모)
├── data/
│   ├── raw/{video_id}.json         ← 실제 수집 원본(메타·설명·댓글·자막)
│   ├── transcripts/{video_id}.txt  ← 캡처한 자막(있으면 요약 근거로 사용)
│   └── analysis/{video_id}.json    ← 편집 요약·민심 분류·인용 spec
└── screenshots/                    ← 렌더링 확인 스크린샷
```

## 2. 실행 방법

```bash
cd kbeauty-trend-prototype

# (1) 새 영상 수집 → (2) 요약 → (3) articles.json 생성
python3 summarize_kbeauty.py all

# 영상 1편만 추가로 수집
python3 summarize_kbeauty.py collect --url https://www.youtube.com/watch?v=XXXXXXXXXXX --pages 4

# 이미 수집한 것만 요약/빌드
python3 summarize_kbeauty.py analyze            # 저장된 편집 분석 사용
python3 summarize_kbeauty.py analyze --llm      # API 키가 있으면 LLM 요약

# 댓글 표본이 바뀌었을 때 인용 재연결
python3 summarize_kbeauty.py relink

# 단일 파일 HTML 재생성 (index-fetch.html + articles.json → index.html)
python3 build_preview.py
```

웹으로 띄워서 보려면 (index-fetch.html은 `articles.json`을 불러옵니다):

```bash
python3 -m http.server 8000
# http://localhost:8000/index-fetch.html
```

### 요약 단계의 두 갈래

| 상황 | 동작 |
|---|---|
| `analyze --llm` + `OPENAI_API_KEY` / `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` | 수집한 자막·댓글을 근거로 영문 제목·요약·민심 분류·인용을 LLM이 생성 (`KBEATY_MODEL`로 모델 지정) |
| 키 없음 (기본값) | `data/analysis/{id}.json`의 **편집(사람) 분석** 사용 — 이 프로토타입의 현재 상태 |
| 분석 파일도 없음 | 자막·댓글만으로 만든 휴리스틱 초안 + `needs_review: true` |

LLM 프롬프트는 `SUMMARY_PROMPT`에 있고, 응답은 `articles.json`과 같은 스키마로 떨어집니다.
→ API 키만 넣으면 자동 요약까지 그대로 돌아갑니다.

## 3. 수집 방식 (중요)

`yt-dlp`는 이 환경에서 유튜브 봇 차단(`Sign in to confirm you're not a bot`)에 걸려
메타데이터·자막 추출이 실패했습니다. 그래서 스크립트는 **공개 페이지만 읽는 방식**으로
직접 구현했습니다.

| 데이터 | 경로 |
|---|---|
| 제목·채널·구독자·조회수·좋아요·업로드일·설명 | 시청 페이지 HTML의 `ytInitialData` 파싱 |
| 한국어 댓글(상위 80건, 좋아요순) | InnerTube `/youtubei/v1/next` 연속 토큰 (로그인 불필요) |
| 자막 | ① `data/transcripts/{id}.txt` 캐시 → ② `yt-dlp` 시도(설치·인증 필요) |

자막은 이 환경에서 `yt-dlp`가 막혀 **브라우저의 '스크립트 표시' 패널로 직접 캡처**해
`data/transcripts/`에 넣었습니다. 캐시가 없는 영상은 설명(제품 목록·가격·챕터)을 근거로
요약하며, 아티클 하단 `Transcript: not captured — description used`로 표시됩니다.

## 4. 아티클 포맷 (고정)

각 아티클은 아래 4개 블록으로 고정됩니다.

1. **The 30-second takeaway** — 왜 한국에서 떴는지 / 추천 피부 타입 / 피해야 할 피부 / 한국 가격
2. **Korean comment sentiment meter** — 긍정 vs 비판 vs 협찬 의심 비율 + 실제 한국어 댓글 원문·영문 번역·좋아요 수·톤 칩
3. **Where to buy in the US** — 아마존 검색/상품 링크(어필리에이트 자리표시자 `tag=YOURTAG-20`) + 주의 성분 한 줄
4. **Source & creator credit** — 원본 영상 제목·채널·구독자·조회수, 유튜브 임베드(클릭 시 로드), 수집 데이터 품질 표기

홈: All / Skincare / Sunscreen / Olive Young Picks 카테고리 필터, 카드 클릭 → 상세 전환(해시 라우팅),
주간 뉴스레터 구독 폼(데모 — 실제 전송 없음).

## 5. 이번 호 수록 영상 (실제 데이터)

| # | 카테고리 | 영상 | 채널 | 조회수 | 게시 |
|---|---|---|---|---|---|
| 1 | Skincare | 2026 수분크림 1위는? 백화점부터 다이소까지 40여종 성분, 효과 비교 추천, 비추천 | 디렉터파이 | 33.4만 | 2026-11-06 |
| 2 | Sunscreen | (Eng) 2026 선크림 1위는? 48종 비교 추천 | 디렉터파이 | 28.7만 | 2026-05-24 |
| 3 | Olive Young Picks | "이게 만원대..?" 가성비 좋은 올리브영 필수템 5가지를 20년차 피부과 전문의가 알려드립니다 | 피부는 성현철 | 175.6만 | 2026-08-30 |
| 4 | Olive Young Picks | [ENG] NO광고 백화점 VIP가 세일 아닐 때도 쟁이는 올리브영 찐템 | 유리하다 | 26.6만 | 2025-08-29 |

각 영상의 실제 URL·설명·댓글 원문은 `data/raw/`에 그대로 남아 있습니다.

## 6. 지금 상태 / 한계

- **민심 수치는 편집 판단입니다.** 80개 상위 댓글을 읽고 긍정/비판/협찬의심으로 분류한 값이며
  (`method` 필드에 명시), 파이프라인에는 키워드 가중 휴리스틱(`heuristic_sentiment`)도 들어 있어
  LLM 없이도 초안을 만들 수 있습니다. 표본이 80건이라 영상 전체 여론의 근사치입니다.
- **인용은 원문 그대로**입니다. 분석 파일에는 `quote_specs`(앞부분 문자열)만 두고,
  빌드 시 실제 수집 댓글과 매칭해 원문·좋아요 수를 채웁니다. 매칭 결과는
  `data/analysis/*.json`의 `comment_quotes`에 고정되며, 댓글 표본이 회전해 사라져도 유지됩니다.
- **유튜브 댓글 표본은 매번 조금씩 달라집니다**(상위 정렬 기준). `relink`로 재연결할 수 있습니다.
- **자막 캡처는 수동 구간이 있습니다.** 2번 영상은 영어 자동 자막 일부 + 챕터 정리만,
  3·4번 영상은 자막 없이 설명·댓글 기반입니다.
- 아마존 링크는 **실제 어필리에이트 태그가 아닙니다**. `index-fetch.html`의 `AMAZON_TAG` 상수를
  실제 태그로 바꾸고 `build_preview.py`를 다시 실행하면 됩니다.
- 뉴스레터 폼은 UI 데모입니다(저장·전송 없음).

## 7. 다음 확장 포인트

1. **LLM 자동화 켜기** — `analyze --llm` + API 키. 프롬프트/스키마는 이미 준비됨.
2. **자막 자동 수집** — 서버 환경에서 `yt-dlp`(쿠키/PO token) 또는 자체 캡션 파서 연결.
3. **댓글 표본 확대** — `collect --pages 8~10` (영상당 150~200건) 후 민심 가중치 재계산.
4. **실제 어필리에이트 연동** — 아마존 어소시에이트 태그 + 상품 ASIN 매핑 테이블.
5. **발행 자동화** — 주 1회 `collect → analyze --llm → build` 스케줄 액션으로 새 호 생성.
6. **SEO 마감** — 아티클별 OG 이미지, 구조화 데이터(Article/FAQ), 사이트맵.
