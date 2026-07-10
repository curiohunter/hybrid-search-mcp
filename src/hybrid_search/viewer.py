"""Local memory viewer — one self-contained HTML page, no server.

Automatic capture earns trust only when the user can *see* what was
captured. ``hybrid-search-mcp viewer`` renders the project's memory —
Q&A timeline, memory cards, corpus stats — into
``.hybrid-search/viewer.html``: plain HTML + inline JS filter, embedded
data, zero network requests. Open it in any browser; nothing leaves the
machine.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class ViewerData:
    project_name: str
    generated_at: str
    qa_entries: list[dict]      # {id, date, query, trigger, client}
    cards: list[dict]           # {id, date, type, summary}
    stats: dict                 # {label: count}


def collect_viewer_data(project_root: Path, project_name: str, stats: dict | None = None) -> ViewerData:
    from hybrid_search.memory import reader

    qa_entries: list[dict] = []
    for idx in reader.iter_qa_indexes(project_root):
        qa_entries.append({
            "id": idx.id,
            "date": idx.timestamp.strftime("%Y-%m-%d %H:%M") if idx.timestamp else "?",
            "query": (idx.query or "").strip()[:300],
            "trigger": idx.trigger or "",
            "client": getattr(idx, "client", "") or "",
        })

    cards: list[dict] = []
    cards_root = project_root / ".hybrid-search" / "memory" / "cards"
    if cards_root.is_dir():
        for md in sorted(cards_root.rglob("*.md"), reverse=True):
            text = md.read_text(encoding="utf-8", errors="replace")
            summary = _frontmatter(text, "summary") or _first_body_line(text)
            cards.append({
                "id": md.stem,
                "date": _date_from_card_path(md),
                "type": _frontmatter(text, "type") or "card",
                "summary": summary[:300],
            })

    return ViewerData(
        project_name=project_name,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        qa_entries=qa_entries,
        cards=cards,
        stats=stats or {},
    )


def _frontmatter(text: str, key: str) -> str:
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end < 0:
        return ""
    for line in text[3:end].splitlines():
        k, sep, v = line.partition(":")
        if sep and k.strip() == key:
            return v.strip().strip('"')
    return ""


def _first_body_line(text: str) -> str:
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end >= 0:
            body = text[end + 4:]
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line
    return ""


def _date_from_card_path(md: Path) -> str:
    # cards/YYYY/MM/DD-HHMMSS-hash.md
    try:
        year, month = md.parts[-3], md.parts[-2]
        day = md.stem.split("-", 1)[0]
        return f"{year}-{month}-{day}"
    except Exception:
        return "?"


def build_viewer_html(data: ViewerData) -> str:
    payload = json.dumps(
        {"qa": data.qa_entries, "cards": data.cards},
        ensure_ascii=False,
    )
    # "</script>" inside a JSON string would terminate the inline <script>
    # block at HTML-parse time — classic breakout. Escaping "</" keeps the
    # JS value identical while making the sequence inert in markup.
    payload = payload.replace("</", "<\\/")
    stats_html = "".join(
        f'<div class="stat"><div class="num">{html.escape(str(v))}</div>'
        f'<div class="lbl">{html.escape(str(k))}</div></div>'
        for k, v in data.stats.items()
    )
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(data.project_name)} — memory viewer</title>
<style>
  :root {{
    --bg: #f6f7f9; --panel: #ffffff; --ink: #1c2430; --dim: #67707e;
    --line: #e3e6ea; --accent: #2563c4; --chip: #eef1f5;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #14171c; --panel: #1c2027; --ink: #dfe4ea; --dim: #8b94a1;
            --line: #2a2f38; --accent: #6ca0f0; --chip: #252a33; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--ink);
         font: 14px/1.6 ui-sans-serif, -apple-system, "Apple SD Gothic Neo", sans-serif; }}
  .wrap {{ max-width: 880px; margin: 0 auto; padding: 32px 20px 64px; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .sub {{ color: var(--dim); font-size: 12.5px; margin-bottom: 24px; }}
  .stats {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 24px; }}
  .stat {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
          padding: 12px 18px; min-width: 110px; }}
  .stat .num {{ font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; }}
  .stat .lbl {{ font-size: 12px; color: var(--dim); }}
  input[type=search] {{ width: 100%; padding: 10px 14px; border: 1px solid var(--line);
    border-radius: 8px; background: var(--panel); color: var(--ink); font-size: 14px;
    margin-bottom: 18px; }}
  .tabs {{ display: flex; gap: 8px; margin-bottom: 14px; }}
  .tab {{ padding: 6px 14px; border-radius: 999px; background: var(--chip);
         color: var(--dim); cursor: pointer; border: none; font-size: 13px; }}
  .tab.on {{ background: var(--accent); color: #fff; }}
  .entry {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
           padding: 12px 16px; margin-bottom: 10px; }}
  .entry .meta {{ font-size: 11.5px; color: var(--dim); margin-bottom: 3px;
                 display: flex; gap: 10px; }}
  .entry .q {{ white-space: pre-wrap; word-break: break-word; }}
  .empty {{ color: var(--dim); text-align: center; padding: 40px 0; }}
  .badge {{ background: var(--chip); border-radius: 4px; padding: 0 6px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>{html.escape(data.project_name)} — Memory Viewer</h1>
  <div class="sub">generated {html.escape(data.generated_at)} · 이 파일은 로컬 전용이며 아무것도 전송하지 않습니다</div>
  <div class="stats">{stats_html}</div>
  <input type="search" id="q" placeholder="기억 검색… (질문 텍스트 필터)">
  <div class="tabs">
    <button class="tab on" data-view="qa">Q&amp;A 타임라인</button>
    <button class="tab" data-view="cards">메모리 카드</button>
  </div>
  <div id="list"></div>
</div>
<script>
  const DATA = {payload};
  let view = "qa";
  const list = document.getElementById("list");
  const q = document.getElementById("q");

  function esc(s) {{
    return (s || "").replace(/[&<>"']/g, c => ({{
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }})[c]);
  }}

  function render() {{
    const needle = q.value.trim().toLowerCase();
    const rows = (view === "qa" ? DATA.qa : DATA.cards).filter(e =>
      !needle || (e.query || e.summary || "").toLowerCase().includes(needle));
    if (!rows.length) {{
      list.innerHTML = '<div class="empty">표시할 항목이 없습니다</div>';
      return;
    }}
    list.innerHTML = rows.map(e => view === "qa"
      ? `<div class="entry"><div class="meta"><span>${{esc(e.date)}}</span>` +
        (e.client ? `<span class="badge">${{esc(e.client)}}</span>` : "") +
        (e.trigger ? `<span class="badge">${{esc(e.trigger)}}</span>` : "") +
        `</div><div class="q">${{esc(e.query)}}</div></div>`
      : `<div class="entry"><div class="meta"><span>${{esc(e.date)}}</span>` +
        `<span class="badge">${{esc(e.type)}}</span></div>` +
        `<div class="q">${{esc(e.summary)}}</div></div>`
    ).join("");
  }}

  q.addEventListener("input", render);
  document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => {{
    document.querySelectorAll(".tab").forEach(x => x.classList.remove("on"));
    t.classList.add("on");
    view = t.dataset.view;
    render();
  }}));
  render();
</script>
</body>
</html>
"""


def write_viewer(project_root: Path, project_name: str, stats: dict | None = None) -> Path:
    data = collect_viewer_data(project_root, project_name, stats)
    out = project_root / ".hybrid-search" / "viewer.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_viewer_html(data), encoding="utf-8")
    return out
