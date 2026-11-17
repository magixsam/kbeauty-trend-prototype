#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
summarize_kbeauty.py — K-뷰티 트렌드 미디어 프로토타입용 수집·요약 파이프라인
=========================================================================

실제 한국 뷰티 유튜브 영상 1개(또는 여러 개)를 입력하면
  1) 메타데이터(제목·채널·조회수·좋아요·업로드일·설명)  — 유튜브 시청 페이지 HTML의 ytInitialData
  2) 실제 한국어 댓글(상위 댓글 + 좋아요 수 + 작성 시점)   — InnerTube /youtubei/v1/next 연속 토큰
  3) 자막(한국어)                                       — 로컬 캐시 파일 또는 yt-dlp(설치·인증 필요)
를 모아 구조화된 JSON으로 저장하고,
  4) 영문 요약·민심 분류를 붙여 articles.json(매거진 데이터)을 만든다.

요약 단계는 두 갈래다.
  · `--llm` + API 키(OPENAI_API_KEY / GEMINI_API_KEY / ANTHROPIC_API_KEY)가 있으면
    수집한 실제 자막·댓글을 근거로 LLM이 영문 요약·민심 분류를 생성한다.
  · 키가 없으면 `data/analysis/{video_id}.json` 에 미리 작성해 둔 편집 분석을 그대로 쓴다.
    (프로토타입 기본 동작 — 사람이 검수한 요약)
  · 둘 다 없을 때는 자막·댓글만으로 만든 '휴리스틱 초안'을 넣고 needs_review=true 로 표시한다.

사용 예:
    python3 summarize_kbeauty.py collect                 # config/videos.json 의 영상 전부 수집
    python3 summarize_kbeauty.py collect --url <URL>     # 영상 1개만 수집
    python3 summarize_kbeauty.py analyze --llm           # (API 키 있을 때) LLM 요약
    python3 summarize_kbeauty.py build                   # articles.json 생성
    python3 summarize_kbeauty.py all                     # collect + analyze + build

네트워크: 유튜브 공개 페이지/공개 API만 읽는다(로그인·쿠키 불필요).
의존성: 표준 라이브러리만. 자막 자동 추출을 원하면 `pip install yt-dlp` (선택).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent
DATA_RAW = ROOT / "data" / "raw"
DATA_TRANSCRIPT = ROOT / "data" / "transcripts"
DATA_ANALYSIS = ROOT / "data" / "analysis"
CONFIG = ROOT / "config" / "videos.json"
ARTICLES = ROOT / "articles.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9", "Accept": "text/html,application/json"}


# ----------------------------------------------------------------------------
# 0. 작은 유틸
# ----------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[summarize_kbeauty] {msg}", file=sys.stderr)


def http_get(url: str, timeout: int = 40) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def http_post_json(url: str, payload: dict, timeout: int = 40) -> dict:
    body = json.dumps(payload).encode("utf-8")
    headers = dict(HEADERS)
    headers.update({"Content-Type": "application/json", "Origin": "https://www.youtube.com",
                    "X-Youtube-Client-Name": "1", "X-Youtube-Client-Version": "2.20261116.00.00"})
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def video_id_from(url_or_id: str) -> str:
    s = url_or_id.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    q = urllib.parse.urlparse(s)
    if q.hostname in {"youtu.be"}:
        return q.path.lstrip("/")[:11]
    m = re.search(r"(?:v=|/shorts/|/embed/)([A-Za-z0-9_-]{11})", s)
    if m:
        return m.group(1)
    raise ValueError(f"유튜브 URL/ID를 해석할 수 없습니다: {url_or_id}")


def deep_find(obj, key):
    """JSON 트리 어디에 있든 key 를 가진 값을 순서대로 yield."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            yield from deep_find(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from deep_find(v, key)


def text_of(node) -> str:
    """runs / simpleText / content 형태의 유튜브 텍스트 노드를 평문화."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if "simpleText" in node:
            return node["simpleText"]
        if "content" in node:
            return node["content"]
        if "runs" in node:
            return "".join(r.get("text", "") for r in node["runs"])
        if "accessibility" in node:
            return text_of(node["accessibility"].get("accessibilityData", {}).get("label"))
    return ""


def to_int(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


# ----------------------------------------------------------------------------
# 1. 수집 — 메타데이터 / 설명
# ----------------------------------------------------------------------------

def fetch_watch_html(vid: str) -> str:
    return http_get(f"https://www.youtube.com/watch?v={vid}&hl=ko&gl=KR")


def parse_watch_page(html: str) -> dict:
    m = re.search(r"var ytInitialData\s*=\s*(\{.+?\});</script>", html, re.S)
    if not m:
        raise RuntimeError("ytInitialData 를 찾지 못했습니다(페이지 구조 변경 또는 차단).")
    data = json.loads(m.group(1))

    primary = next(deep_find(data, "videoPrimaryInfoRenderer"), {})
    secondary = next(deep_find(data, "videoSecondaryInfoRenderer"), {})

    title = text_of(primary.get("title"))
    views_text = text_of(primary.get("viewCount", {}).get("videoViewCountRenderer", {}).get("viewCount"))
    published = text_of(primary.get("dateText"))

    owner = secondary.get("owner", {}).get("videoOwnerRenderer", {})
    channel = text_of(owner.get("title"))
    subs = text_of(owner.get("subscriberCountText"))
    channel_id = (owner.get("navigationEndpoint", {})
                  .get("browseEndpoint", {})
                  .get("browseId", ""))
    channel_handle = (owner.get("navigationEndpoint", {})
                      .get("browseEndpoint", {})
                      .get("canonicalBaseUrl", ""))

    desc = text_of(secondary.get("attributedDescription")) or text_of(secondary.get("description"))
    if not desc:
        dd = next(deep_find(secondary, "attributedDescriptionBodyText"), {})
        desc = text_of(dd)

    likes = None
    for label in deep_find(primary, "accessibilityText"):
        if isinstance(label, str) and "좋아요" in label:
            likes = to_int(label)
            if likes:
                break

    comment_count = None
    for t in deep_find(data, "contextualInfo"):
        v = text_of(t)
        if "댓글" in v:
            comment_count = to_int(v)
            break

    return {
        "video_id": None,  # 호출부에서 채움
        "title": title,
        "channel": channel,
        "channel_id": channel_id,
        "channel_handle": channel_handle.lstrip("/") or None,
        "subscribers_text": subs,
        "views_text": views_text,
        "views": to_int(views_text),
        "likes": likes,
        "published_text": published,
        "comment_count": comment_count,
        "description": desc,
        "source_url": None,
    }


# ----------------------------------------------------------------------------
# 2. 수집 — 댓글 (InnerTube next)
# ----------------------------------------------------------------------------

INNERTUBE_KEY_RE = re.compile(r'"INNERTUBE_API_KEY":"([^"]+)"')
INNERTUBE_VER_RE = re.compile(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"')


def comment_continuation_tokens(data: dict) -> list[str]:
    tokens: list[str] = []
    for panel in deep_find(data, "itemSectionRenderer"):
        blob = json.dumps(panel, ensure_ascii=False)
        if "comment-item-section" not in blob and "commentSectionRenderer" not in blob:
            continue
        for tok in deep_find(panel, "continuationCommand"):
            t = tok.get("token")
            if t and t not in tokens:
                tokens.append(t)
    if not tokens:  # 폴백: 전체 트리에서 comment 관련 연속 토큰
        for cir in deep_find(data, "continuationItemRenderer"):
            blob = json.dumps(cir, ensure_ascii=False)
            if "comment" not in blob.lower():
                continue
            t = cir.get("continuationEndpoint", {}).get("continuationCommand", {}).get("token")
            if t and t not in tokens:
                tokens.append(t)
    return tokens


def build_comments_response(resp: dict) -> tuple[list[dict], list[str]]:
    """댓글 엔티티(payload.commentEntityPayload) + 다음 페이지 토큰 후보들."""
    comments: list[dict] = []
    for mut in (resp.get("frameworkUpdates", {})
                    .get("entityBatchUpdate", {})
                    .get("mutations", [])):
        cep = mut.get("payload", {}).get("commentEntityPayload")
        if not cep:
            continue
        props = cep.get("properties", {})
        toolbar = cep.get("toolbar", {})
        comments.append({
            "comment_id": props.get("commentId"),
            "author": (cep.get("author") or {}).get("displayName"),
            "text": (props.get("content") or {}).get("content", ""),
            "published_time": props.get("publishedTime"),
            "like_count": to_int(toolbar.get("likeCountLiked") or ""),
            "reply_count": to_int(toolbar.get("replyCount") or ""),
            "is_reply": bool(props.get("replyLevel")),
        })

    # 다음 페이지 토큰: appendContinuationItemsAction 의 마지막 continuationItemRenderer 를 우선 사용하고,
    # 없으면 트리 안의 마지막 continuationItemRenderer 토큰을 쓴다.
    candidates: list[str] = []
    for action in deep_find(resp, "appendContinuationItemsAction"):
        items = action.get("continuationItems") or []
        for item in reversed(items):
            if isinstance(item, dict) and "continuationItemRenderer" in item:
                tok = (item["continuationItemRenderer"].get("continuationEndpoint", {})
                       .get("continuationCommand", {}).get("token"))
                if tok:
                    candidates.append(tok)
                break
    for cir in deep_find(resp, "continuationItemRenderer"):
        tok = cir.get("continuationEndpoint", {}).get("continuationCommand", {}).get("token")
        if tok:
            candidates.append(tok)
    seen_tok, uniq = set(), []
    for t in candidates:
        if t not in seen_tok:
            seen_tok.add(t)
            uniq.append(t)
    return comments, uniq


def fetch_comments(html: str, data: dict, max_pages: int = 3) -> tuple[list[dict], list[str]]:
    key_m = INNERTUBE_KEY_RE.search(html)
    ver_m = INNERTUBE_VER_RE.search(html)
    if not key_m:
        return [], ["INNERTUBE_API_KEY 를 찾지 못해 댓글 수집을 건너뜁니다."]

    key = key_m.group(1)
    ver = ver_m.group(1) if ver_m else "2.20260101.00.00"
    endpoint = f"https://www.youtube.com/youtubei/v1/next?key={key}&prettyPrint=false"
    notes: list[str] = []

    tokens = comment_continuation_tokens(data)
    if not tokens:
        return [], ["댓글 섹션 연속 토큰을 찾지 못했습니다(댓글 사용 중지 영상일 수 있음)."]

    out: list[dict] = []
    seen_ids: set[str] = set()
    visited: set[str] = set()
    queue: list[str] = list(tokens)

    def post(token: str) -> dict | None:
        payload = {"context": {"client": {"clientName": "WEB", "clientVersion": ver,
                                          "hl": "ko", "gl": "KR"}},
                   "continuation": token}
        try:
            return http_post_json(endpoint, payload)
        except urllib.error.HTTPError as e:
            notes.append(f"연속 요청 실패: HTTP {e.code}")
            return None

    pages = 0
    while queue and pages < max_pages:
        token = queue.pop(0)
        if token in visited:
            continue
        visited.add(token)
        resp = post(token)
        if resp is None:
            continue
        page_comments, next_tokens = build_comments_response(resp)
        page_comments = [c for c in page_comments if not c["is_reply"] and c["comment_id"] not in seen_ids]
        if len(page_comments) < 3:
            # 답글 스트림이거나 빈 페이지 — 이 가지는 버리고 남은 후보로 넘어간다.
            queue.extend(t for t in next_tokens if t not in visited)
            continue
        for c in page_comments:
            seen_ids.add(c["comment_id"])
        out.extend(page_comments)
        pages += 1
        queue = [t for t in next_tokens if t not in visited] + queue
        time.sleep(0.6)
    # 중복 제거
    seen, uniq = set(), []
    for c in out:
        if c["comment_id"] in seen:
            continue
        seen.add(c["comment_id"])
        uniq.append(c)
    uniq.sort(key=lambda c: (c["like_count"] or 0), reverse=True)
    return uniq, notes


# ----------------------------------------------------------------------------
# 3. 수집 — 자막
# ----------------------------------------------------------------------------

def read_cached_transcript(vid: str) -> str | None:
    for name in (f"{vid}.ko.txt", f"{vid}.txt", f"{vid}.ko.srt"):
        p = DATA_TRANSCRIPT / name
        if p.exists():
            return p.read_text(encoding="utf-8")
    return None


def try_ytdlp_transcript(vid: str) -> str | None:
    """yt-dlp 로 자동 자막을 받아 본다(설치돼 있고 유튜브가 허용할 때만 성공)."""
    exe = shutil.which("yt-dlp") or str(Path.home() / ".local" / "bin" / "yt-dlp")
    if not Path(exe).exists():
        return None
    tmp = DATA_TRANSCRIPT / f".ytdlp-{vid}"
    tmp.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "--skip-download", "--write-auto-subs", "--write-subs",
           "--sub-langs", "ko.*,ko", "--sub-format", "vtt",
           "-o", str(tmp / "%(id)s.%(ext)s"),
           f"https://www.youtube.com/watch?v={vid}"]
    try:
        subprocess.run(cmd, capture_output=True, timeout=180, check=False)
    except Exception as e:  # noqa: BLE001
        log(f"yt-dlp 실행 실패: {e}")
        return None
    files = sorted(tmp.glob("*.vtt"))
    if not files:
        return None
    raw = files[0].read_text(encoding="utf-8", errors="replace")
    lines = []
    for chunk in raw.split("\n\n"):
        body = [ln for ln in chunk.strip().split("\n") if ln and "-->" not in ln and not ln.startswith("WEBVTT")]
        if body:
            lines.append(" ".join(body))
    return dedupe_vtt("\n".join(lines))


def dedupe_vtt(text: str) -> str:
    """자동 자막의 겹침 반복을 단순 제거."""
    out, prev = [], ""
    for line in text.split("\n"):
        line = re.sub(r"<[^>]+>", "", line).strip()
        if not line or line == prev:
            continue
        out.append(line)
        prev = line
    return "\n".join(out)


def fetch_transcript(vid: str, allow_ytdlp: bool = True) -> tuple[str, str]:
    cached = read_cached_transcript(vid)
    if cached:
        return cached, "cache"
    if allow_ytdlp:
        got = try_ytdlp_transcript(vid)
        if got:
            (DATA_TRANSCRIPT / f"{vid}.ko.txt").write_text(got, encoding="utf-8")
            return got, "yt-dlp"
    return "", "missing"


# ----------------------------------------------------------------------------
# 4. 민심 휴리스틱 (LLM 없이 쓰는 기본 스코어러)
# ----------------------------------------------------------------------------

POSITIVE_MARKERS = ["좋아요", "최고", "감사", "꿀", "미쳤", "찐", "대박", "믿고", "촉촉", "진정",
                    "재구매", "인생템", "추천", "만족", "사랑", "짱", "완벽", "기다렸", "유용"]
NEGATIVE_MARKERS = ["별로", "아쉽", "실망", "안 좋", "불편", "비싸", "자극", "트러블", "안 맞", "후회",
                    "최악", "과장", "낚시", "환불", "따가", "건조", "번들", "밀림", "어렵", "이해가 안"]
AD_MARKERS = ["광고", "협찬", "내돈내산", "바이럴", "PPL", "뒷광고", "체험단", "서포터즈"]


def heuristic_sentiment(comments: list[dict]) -> dict:
    pos = neg = ad = 0
    weight_total = 0.0
    for c in comments:
        t = c["text"]
        w = 1 + min((c.get("like_count") or 0) / 100.0, 3.0)
        weight_total += w
        score_p = sum(1 for k in POSITIVE_MARKERS if k in t)
        score_n = sum(1 for k in NEGATIVE_MARKERS if k in t)
        has_ad = any(k in t for k in AD_MARKERS)
        if has_ad:
            ad += w
        if score_p > score_n:
            pos += w
        elif score_n > score_p:
            neg += w
    total = pos + neg + ad
    if total <= 0:
        return {"positive_pct": 0, "negative_pct": 0, "ad_suspicion_pct": 0,
                "method": "heuristic", "sample_size": len(comments)}
    return {
        "positive_pct": round(pos / total * 100),
        "negative_pct": round(neg / total * 100),
        "ad_suspicion_pct": round(ad / total * 100),
        "method": "heuristic",
        "sample_size": len(comments),
    }


# ----------------------------------------------------------------------------
# 5. LLM 요약 (선택)
# ----------------------------------------------------------------------------

SUMMARY_PROMPT = """You are the editor of an English-language K-beauty magazine for US readers aged 20-39.
You are given REAL data for one Korean YouTube video: description, Korean transcript (auto captions, may be noisy)
and real Korean comments with like counts.

Write ONLY JSON with this shape:
{{
  "seo_title": "question-style English headline, <= 70 chars, includes the product/trend keyword",
  "dek": "one-sentence English subtitle, <= 140 chars",
  "takeaway": {{
     "why_korea": "2-3 sentences on why this blew up in Korea",
     "best_for": "skin types / concerns it suits",
     "skip_if": "who should skip it",
     "korean_price_krw": "price(s) as in the source, or empty string"
  }},
  "products": [{{"name_en": "...", "name_ko": "...", "why": "<= 20 words"}}],
  "sentiment": {{"positive_pct": int, "negative_pct": int, "ad_suspicion_pct": int,
                 "note": "one line on what the split means"}},
  "comment_quotes": [{{"ko": "verbatim Korean comment", "en": "natural English translation",
                       "likes": int, "tone": "positive|critical|ad-skeptic"}}],
  "caveat": "one-line ingredient or usage caution for US buyers",
  "us_search_terms": ["amazon search phrase 1", "amazon search phrase 2"]
}}
Rules: quote comments VERBATIM from the comments given (never invent). Do not translate brand names.
Choose 3-4 quotes that show a real split of opinion. All prose in English."""


def llm_available() -> tuple[str, str] | tuple[None, None]:
    if os.environ.get("OPENAI_API_KEY"):
        return "openai", os.environ["OPENAI_API_KEY"]
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "gemini", os.environ.get("GEMINI_API_KEY") or os.environ["GOOGLE_API_KEY"]
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic", os.environ["ANTHROPIC_API_KEY"]
    return None, None


def call_llm(provider: str, key: str, prompt: str) -> str:
    if provider == "openai":
        body = {"model": os.environ.get("KBEATY_MODEL", "gpt-4o-mini"),
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.4,
                "response_format": {"type": "json_object"}}
        req = urllib.request.Request("https://api.openai.com/v1/chat/completions",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {key}"}, method="POST")
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.loads(r.read().decode())
        return out["choices"][0]["message"]["content"]

    if provider == "gemini":
        model = os.environ.get("KBEATY_MODEL", "gemini-2.0-flash")
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
               f"?key={key}")
        body = {"contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4}}
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.loads(r.read().decode())
        return out["candidates"][0]["content"]["parts"][0]["text"]

    if provider == "anthropic":
        body = {"model": os.environ.get("KBEATY_MODEL", "claude-3-5-haiku-20241022"),
                "max_tokens": 2000, "temperature": 0.4,
                "messages": [{"role": "user", "content": prompt}]}
        req = urllib.request.Request("https://api.anthropic.com/v1/messages",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              "x-api-key": key, "anthropic-version": "2023-06-01"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.loads(r.read().decode())
        return out["content"][0]["text"]

    raise ValueError(provider)


def analysis_prompt_for(raw: dict) -> str:
    comments = raw.get("comments", [])[:40]
    comment_blob = "\n".join(
        f"- [{c.get('like_count') or 0} likes] {c['text']}" for c in comments)
    transcript = raw.get("transcript", "")
    if len(transcript) > 12000:
        transcript = transcript[:12000] + "\n...(truncated)"
    return (SUMMARY_PROMPT
            + "\n\n=== VIDEO ===\n"
            + f"title: {raw['title']}\nchannel: {raw['channel']}\nviews: {raw['views_text']}\n"
            + f"published: {raw['published_text']}\n\n=== DESCRIPTION ===\n{raw.get('description','')[:4000]}\n"
            + f"\n=== KOREAN TRANSCRIPT ===\n{transcript}\n"
            + f"\n=== KOREAN COMMENTS ({len(comments)}) ===\n{comment_blob}\n")


# ----------------------------------------------------------------------------
# 6. 명령들
# ----------------------------------------------------------------------------

def load_config() -> list[dict]:
    if CONFIG.exists():
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    return []


def cmd_collect(args) -> None:
    DATA_RAW.mkdir(parents=True, exist_ok=True)
    DATA_TRANSCRIPT.mkdir(parents=True, exist_ok=True)

    targets: list[dict] = []
    if args.url:
        for u in args.url:
            targets.append({"id": video_id_from(u), "url": u})
    else:
        targets = load_config()

    if not targets:
        log("수집할 영상이 없습니다. config/videos.json 을 만들거나 --url 을 주세요.")
        return

    for t in targets:
        vid = video_id_from(t.get("id") or t["url"])
        log(f"수집: {vid}")
        html = fetch_watch_html(vid)
        meta = parse_watch_page(html)
        meta["video_id"] = vid
        meta["source_url"] = f"https://www.youtube.com/watch?v={vid}"
        meta["collected_at"] = datetime.now(KST).isoformat(timespec="seconds")
        meta["category"] = t.get("category")
        meta["topic_note"] = t.get("note")

        data = json.loads(re.search(r"var ytInitialData\s*=\s*(\{.+?\});</script>", html, re.S).group(1))
        comments, notes = fetch_comments(html, data, max_pages=args.pages)
        meta["comments"] = comments
        meta["comment_notes"] = notes

        transcript, source = fetch_transcript(vid, allow_ytdlp=not args.no_ytdlp)
        meta["transcript"] = transcript
        meta["transcript_source"] = source
        if source == "missing":
            log(f"  자막 없음 — data/transcripts/{vid}.ko.txt 에 넣으면 요약 근거로 쓰입니다.")

        (DATA_RAW / f"{vid}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"  저장: data/raw/{vid}.json (댓글 {len(comments)}건, 자막 {len(transcript)}자, 출처 {source})")


def cmd_analyze(args) -> None:
    DATA_ANALYSIS.mkdir(parents=True, exist_ok=True)
    raws = sorted(DATA_RAW.glob("*.json"))
    if not raws:
        log("data/raw 에 수집 결과가 없습니다. 먼저 collect 를 실행하세요.")
        return

    provider, key = (None, None)
    if args.llm:
        provider, key = llm_available()
        if not provider:
            log("LLM API 키가 없습니다(OPENAI_API_KEY / GEMINI_API_KEY / ANTHROPIC_API_KEY). "
                "data/analysis 의 수동 분석을 사용합니다.")

    for path in raws:
        raw = json.loads(path.read_text(encoding="utf-8"))
        vid = raw["video_id"]
        out_path = DATA_ANALYSIS / f"{vid}.json"

        if provider and key:
            log(f"LLM 요약: {vid} ({provider})")
            try:
                text = call_llm(provider, key, analysis_prompt_for(raw))
                analysis = json.loads(text)
                analysis["_source"] = f"llm:{provider}"
                out_path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
                continue
            except Exception as e:  # noqa: BLE001
                log(f"  LLM 실패({e}) — 저장된 분석/휴리스틱으로 폴백")

        if out_path.exists():
            a = json.loads(out_path.read_text(encoding="utf-8"))
            if not a.get("sentiment") or a["sentiment"].get("method") != "heuristic":
                log(f"  편집 분석 사용: data/analysis/{vid}.json")
                continue

        # 휴리스틱 초안
        draft = {
            "_source": "heuristic",
            "needs_review": True,
            "seo_title": raw["title"],
            "dek": "",
            "takeaway": {"why_korea": "", "best_for": "", "skip_if": "", "korean_price_krw": ""},
            "products": [],
            "sentiment": heuristic_sentiment(raw.get("comments", [])),
            "comment_quotes": [
                {"ko": c["text"], "en": "", "likes": c.get("like_count") or 0, "tone": "positive"}
                for c in raw.get("comments", [])[:4]
            ],
            "caveat": "",
            "us_search_terms": [],
        }
        out_path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"  휴리스틱 초안 저장(검수 필요): data/analysis/{vid}.json")


def resolve_quotes(analysis: dict, raw: dict) -> list[dict]:
    """분석 파일의 quote_specs 를 실제 수집 댓글과 연결한다.

    같은 댓글을 편집자가 손으로 옮겨적지 않고 원문 그대로 인용하기 위한 장치.
    spec = {"match": 댓글 앞부분 문자열, "en": 영문 번역, "tone": positive|critical|ad-skeptic}
    """
    comments = raw.get("comments", [])
    quotes: list[dict] = []
    for spec in analysis.get("quote_specs", []):
        needle = (spec.get("match") or "").strip()
        hit = next((c for c in comments if needle and c["text"].strip().startswith(needle)), None)
        if hit is None:
            hit = next((c for c in comments if needle and needle in c["text"]), None)
        if hit is None:
            log(f"  경고: 인용 매칭 실패 — \"{needle[:40]}\"")
            continue
        quotes.append({
            "ko": hit["text"],
            "en": spec.get("en", ""),
            "likes": hit.get("like_count") or 0,
            "author": hit.get("author"),
            "tone": spec.get("tone", "positive"),
            "label": spec.get("label"),
            "note": spec.get("note"),
        })
    if quotes:
        return quotes
    return analysis.get("comment_quotes", [])


def build_article(raw: dict, analysis: dict) -> dict:
    sentiment = analysis.get("sentiment") or heuristic_sentiment(raw.get("comments", []))
    if "sample_size" not in sentiment:
        sentiment["sample_size"] = len(raw.get("comments", []))
    return {
        "id": raw["video_id"],
        "category": raw.get("category") or "Skincare",
        "title": analysis.get("seo_title") or raw["title"],
        "title_ko": raw["title"],
        "dek": analysis.get("dek", ""),
        "hero_emoji": analysis.get("hero_emoji", "🧴"),
        "source": {
            "video_id": raw["video_id"],
            "url": raw["source_url"],
            "embed_url": f"https://www.youtube.com/embed/{raw['video_id']}",
            "title": raw["title"],
            "channel": raw["channel"],
            "channel_handle": raw.get("channel_handle"),
            "subscribers": raw.get("subscribers_text"),
            "views": raw.get("views"),
            "views_text": raw.get("views_text"),
            "likes": raw.get("likes"),
            "published": raw.get("published_text"),
        },
        "takeaway": analysis.get("takeaway", {}),
        "products": analysis.get("products", []),
        "sentiment": sentiment,
        "comment_quotes": (analysis.get("comment_quotes") or resolve_quotes(analysis, raw)),
        "caveat": analysis.get("caveat", ""),
        "us_search_terms": analysis.get("us_search_terms", []),
        "body": analysis.get("body", []),
        "data_quality": {
            "comments_collected": len(raw.get("comments", [])),
            "transcript_source": raw.get("transcript_source"),
            "transcript_chars": len(raw.get("transcript", "")),
            "analysis_source": analysis.get("_source", "editorial"),
            "needs_review": bool(analysis.get("needs_review")),
        },
    }


def cmd_relink(args) -> None:
    """분석 파일의 quote_specs 를 현재 수집된 댓글과 다시 연결해 comment_quotes 로 굳힌다.

    댓글 표본은 유튜브가 돌려주는 순서에 따라 조금씩 바뀌므로, 릴리스 시점의
    인용을 고정해 두고 필요할 때만 다시 연결한다.
    """
    for path in sorted(DATA_ANALYSIS.glob("*.json")):
        vid = path.stem
        raw_path = DATA_RAW / f"{vid}.json"
        if not raw_path.exists():
            log(f"  건너뜀(수집 데이터 없음): {vid}")
            continue
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        analysis = json.loads(path.read_text(encoding="utf-8"))
        if not analysis.get("quote_specs"):
            continue
        quotes = resolve_quotes(analysis, raw)
        if quotes:
            # 이미 고정된 인용 중 이번 수집에 없어진 것은 그대로 둔다(댓글 표본은 회전한다).
            resolved_ko = {q["ko"] for q in quotes}
            kept = [q for q in analysis.get("comment_quotes", [])
                    if q.get("ko") and q["ko"] not in resolved_ko and q.get("spec_match")]
            analysis["comment_quotes"] = quotes + kept
            path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"  인용 {len(analysis['comment_quotes'])}건 고정: data/analysis/{vid}.json")


def cmd_build(args) -> None:
    raws = sorted(DATA_RAW.glob("*.json"))
    if not raws:
        log("data/raw 에 수집 결과가 없습니다.")
        return

    order = [v.get("id") for v in load_config()]
    articles = []
    for path in raws:
        raw = json.loads(path.read_text(encoding="utf-8"))
        a_path = DATA_ANALYSIS / f"{raw['video_id']}.json"
        if not a_path.exists():
            log(f"  분석 없음({raw['video_id']}) — 휴리스틱으로 대체")
            analysis = {"_source": "heuristic", "needs_review": True}
        else:
            analysis = json.loads(a_path.read_text(encoding="utf-8"))
        articles.append(build_article(raw, analysis))

    def sort_key(a):
        try:
            return order.index(a["id"])
        except ValueError:
            return 99
    articles.sort(key=sort_key)

    payload = {
        "site": {
            "name": "SEOUL SCAN",
            "tagline": "K-beauty trends, read straight from Korean comments.",
            "edition": "Prototype Edition · " + datetime.now(KST).strftime("%B %Y"),
            "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
            "locale": "en-US",
        },
        "categories": ["All", "Skincare", "Sunscreen", "Olive Young Picks"],
        "articles": articles,
    }
    ARTICLES.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"articles.json 생성: {len(articles)}개 아티클")


def main() -> None:
    ap = argparse.ArgumentParser(description="K-beauty YouTube → magazine article pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("collect", help="영상 메타데이터·댓글·자막 수집")
    p.add_argument("--url", nargs="*", help="유튜브 URL 또는 영상 ID")
    p.add_argument("--pages", type=int, default=3, help="댓글 페이지 수(기본 3)")
    p.add_argument("--no-ytdlp", action="store_true", help="yt-dlp 자막 시도 생략")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("analyze", help="영문 요약·민심 분류 생성")
    p.add_argument("--llm", action="store_true", help="API 키가 있으면 LLM 사용")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("relink", help="quote_specs 를 현재 댓글과 다시 연결해 고정")
    p.set_defaults(func=cmd_relink)

    p = sub.add_parser("build", help="articles.json 생성")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("all", help="collect + analyze + build")
    p.set_defaults(func=None)

    args = ap.parse_args()
    if args.cmd == "all":
        ns = argparse.Namespace(url=None, pages=4, no_ytdlp=False)
        cmd_collect(ns)
        cmd_analyze(argparse.Namespace(llm=False))
        cmd_build(args)
        return
    args.func(args)


if __name__ == "__main__":
    main()
