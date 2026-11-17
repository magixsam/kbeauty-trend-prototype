#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_preview.py — index-fetch.html + articles.json 을 하나의 오프라인 HTML 파일로 묶는다.

    python3 build_preview.py

결과: index.html (GitHub Pages가 그대로 서비스하는 라이브 페이지)
  · articles.json 데이터를 인라인으로 심어 두기 때문에 서버 없이 파일을 열어도
    모든 아티클과 상세 화면이 그대로 동작한다(모바일/PC 모두).
  · GitHub Pages 배포본에서는 fetch용 원본 템플릿을 index-fetch.html 로 두고,
    빌드 결과를 index.html 로 내보낸다.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "index-fetch.html"
DATA = ROOT / "articles.json"
OUT = ROOT / "index.html"
KST = timezone(timedelta(hours=9))

PLACEHOLDER = '<script id="kbeauty-data" type="application/json">null</script>'


def main() -> None:
    if not SRC.exists():
        raise SystemExit("index-fetch.html 이 없습니다.")
    if not DATA.exists():
        raise SystemExit("articles.json 이 없습니다. 먼저 `python3 summarize_kbeauty.py build` 를 실행하세요.")

    html = SRC.read_text(encoding="utf-8")
    payload = json.loads(DATA.read_text(encoding="utf-8"))
    payload.setdefault("site", {})
    payload["site"]["bundled_at"] = datetime.now(KST).isoformat(timespec="seconds")

    # </script> 로 조기 종료되지 않도록 이스케이프
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    inline = f'<script id="kbeauty-data" type="application/json">{blob}</script>'

    if PLACEHOLDER not in html:
        # 이미 번들된 파일을 다시 번들하는 경우도 처리
        html = re.sub(
            r'<script id="kbeauty-data" type="application/json">.*?</script>',
            inline, html, count=1, flags=re.S)
    else:
        html = html.replace(PLACEHOLDER, inline, 1)

    html = html.replace(
        "<title>SEOUL SCAN",
        f"<!-- bundled {datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')} · offline single-file build -->\n<title>SEOUL SCAN",
        1)

    OUT.write_text(html, encoding="utf-8")
    size_kb = OUT.stat().st_size / 1024
    print(f"생성: {OUT.name} ({size_kb:.0f} KB, 아티클 {len(payload.get('articles', []))}개)")


if __name__ == "__main__":
    main()
