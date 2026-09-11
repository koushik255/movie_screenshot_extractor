#!/usr/bin/env python3
"""Build a static HTML page from torrents-cache.jsonl for browsing movies
and verifying torrent picks on their 1337x / Pirate Bay posting pages.
"""

from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

SIZE_UNITS = {"": 1, "B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def size_bytes(text: str) -> int:
    match = re.search(r"([\d.]+)\s*([KMGT]?)i?B", str(text), re.IGNORECASE)
    if not match:
        return 0
    return int(float(match.group(1)) * SIZE_UNITS.get(match.group(2).upper(), 1))


def esc(text: object) -> str:
    return html.escape(str(text if text is not None else ""))


def build(cache: Path, movie_list: Path, out: Path) -> None:
    done: dict[int, dict] = {}
    if cache.exists():
        for line in cache.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and "rank" in record:
                done[record["rank"]] = record

    movies = []
    for line in movie_list.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(". ", 1)
        if len(parts) == 2 and parts[0].isdigit():
            movies.append((int(parts[0]), parts[1].strip()))

    rows = []
    found = 0
    for rank, title in sorted(movies):
        r = done.get(rank, {})
        has = r.get("magnet") not in (None, "Na", "") and r.get("status") == "ok"
        if has:
            found += 1
        page = r.get("page_url") or r.get("detail_url") or ""
        source = r.get("source", "1337x" if page and "1377x" in page else "")
        if has:
            status_html = (
                f'<a class="page" href="{esc(page)}" target="_blank">'
                f'{"1337x page" if "1377x" in page else "TPB page"}</a>'
                if page
                else '<span class="missing">no page link</span>'
            )
            row_class = ""
            magnet_html = f'<a class="magnet" href="{esc(r["magnet"])}">magnet</a>'
            meta = f"{esc(r.get('seeders', '?'))} seeders · {esc(r.get('size', '?'))}"
            name = esc(r.get("torrent_name", ""))
        else:
            row_class = " class=\"missing-row\""
            status_html = '<span class="badge">no pick</span>'
            magnet_html = ""
            meta = esc(r.get("status", "not searched"))
            name = ""
        rows.append(
            f"<tr{row_class} data-size='{size_bytes(r.get('size', 0)) if has else 0}'>"
            f"<td>{rank}</td>"
            f"<td class='title'>{esc(title)}</td>"
            f"<td class='tname'>{name}</td>"
            f"<td class='meta'>{meta}</td>"
            f"<td>{status_html}</td>"
            f"<td>{magnet_html}</td>"
            f"<td class='chk'><input type='checkbox' class='check' "
            f"data-rank='{rank}' title='mark as reviewed'></td></tr>"
        )

    total = len(movies)
    page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Letterboxd 500 — torrent picks</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
         margin: 2rem auto; max-width: 72rem; padding: 0 1rem;
         color: #e8e6e3; background: #17181c; }}
  h1 {{ font-size: 1.4rem; margin-bottom: .2rem; }}
  p.sub {{ color: #9a96a0; margin-top: 0; }}
  #filter {{ width: 100%; padding: .6rem .9rem; font-size: 1rem;
             border-radius: .5rem; border: 1px solid #33353d;
             background: #1f2127; color: inherit; margin: 1rem 0; }}
  table {{ border-collapse: collapse; width: 100%; font-size: .92rem; }}
  th {{ text-align: left; color: #9a96a0; font-weight: 600;
        padding: .5rem .6rem; border-bottom: 1px solid #33353d;
        position: sticky; top: 0; background: #17181c; }}
  td {{ padding: .45rem .6rem; border-bottom: 1px solid #23252b;
        vertical-align: top; }}
  tr:hover td {{ background: #1f2127; }}
  td.title {{ font-weight: 600; }}
  td.tname {{ color: #b9b5bf; font-family: monospace; font-size: .82rem; }}
  td.meta {{ color: #9a96a0; white-space: nowrap; }}
  td.rank {{ color: #9a96a0; text-align: right; white-space: nowrap; }}
  a.page {{ color: #6ea8ff; text-decoration: none; white-space: nowrap; }}
  a.page:hover {{ text-decoration: underline; }}
  a.magnet {{ color: #7fd78f; text-decoration: none; white-space: nowrap; }}
  a.magnet:hover {{ text-decoration: underline; }}
  .badge {{ color: #b0722e; font-style: italic; }}
  tr.missing-row td {{ opacity: .55; }}
  .count {{ color: #9a96a0; margin: .5rem 0 1rem; }}
  td.chk {{ text-align: center; }}
  input.check {{ width: 1.15rem; height: 1.15rem; accent-color: #7fd78f;
                 cursor: pointer; }}
  tr.checked td {{ opacity: .45; text-decoration: line-through; }}
  tr.checked td.chk {{ opacity: 1; text-decoration: none; }}
  .done-note {{ color: #7fd78f; }}
</style>
</head>
<body>
<h1>Letterboxd 500 — torrent picks</h1>
<p class="sub">best torrent per movie: &le; 4 GB, &ge; 10 seeders, highest seeders
first · open the page link to verify, then click magnet</p>
<div class="count">{found} / {total} movies have a pick ·
<span id="total-gb" class="done-note"></span> ·
<span id="checked-gb" class="done-note"></span> ·
<span id="checked-count" class="done-note">0 reviewed</span> ·
<button id="clear" style="margin-left:.5rem; background:none; color:#9a96a0;
border:1px solid #33353d; border-radius:.4rem; cursor:pointer;
font-size:.8rem; padding:.2rem .6rem;">clear reviews</button></div>
<input id="filter" type="search" placeholder="filter by title or torrent name…">
<table>
<thead><tr>
<th style="width:3em">#</th><th>Movie</th><th>Torrent</th>
<th>Stats</th><th>Verify</th><th>Download</th><th style="width:2.5em">✓</th>
</tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody>
</table>
<script>
const KEY = 'lb500-reviewed';
const reviewed = new Set(JSON.parse(localStorage.getItem(KEY) || '[]'));

function fmtBytes(b) {{
  if (b >= 1024**4) return (b / 1024**4).toFixed(2) + ' TB';
  if (b >= 1024**3) return (b / 1024**3).toFixed(1) + ' GB';
  return (b / 1024**2).toFixed(0) + ' MB';
}}

function totalGb(selector) {{
  let sum = 0;
  document.querySelectorAll(selector).forEach(tr =>
    sum += parseInt(tr.dataset.size || '0', 10));
  return sum;
}}

function sync() {{
  document.querySelectorAll('input.check').forEach(cb => {{
    const tr = cb.closest('tr');
    const on = reviewed.has(cb.dataset.rank);
    cb.checked = on;
    tr.classList.toggle('checked', on);
  }});
  document.getElementById('total-gb').textContent =
    'total size: ' + fmtBytes(totalGb('tbody tr:not(.missing-row)'));
  document.getElementById('checked-gb').textContent =
    'reviewed: ' + fmtBytes(totalGb('tbody tr.checked'));
  document.getElementById('checked-count').textContent =
    reviewed.size + ' reviewed';
}}

document.querySelectorAll('input.check').forEach(cb =>
  cb.addEventListener('change', () => {{
    cb.checked ? reviewed.add(cb.dataset.rank) : reviewed.delete(cb.dataset.rank);
    localStorage.setItem(KEY, JSON.stringify([...reviewed]));
    sync();
  }}));

document.getElementById('clear').addEventListener('click', () => {{
  if (!confirm('Clear all ' + reviewed.size + ' reviewed marks?')) return;
  reviewed.clear();
  localStorage.removeItem(KEY);
  sync();
}});

sync();
const box = document.getElementById('filter');
box.addEventListener('input', () => {{
  const q = box.value.toLowerCase();
  for (const tr of document.querySelectorAll('tbody tr')) {{
    tr.style.display = tr.textContent.toLowerCase().includes(q) ? '' : 'none';
  }}
}});
</script>
</body>
</html>
"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page_html, encoding="utf-8")
    print(f" wrote {out} ({found}/{total} picks)", file=sys.stderr)


if __name__ == "__main__":
    build(
        Path("torrents-cache.jsonl"),
        Path("movies-500.txt"),
        Path("site/index.html"),
    )
