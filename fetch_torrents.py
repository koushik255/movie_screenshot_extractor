#!/usr/bin/env python3
"""Pick the best torrent for each movie in a ranked list, using the
1337x-API scraping code (https://github.com/dhimanparas20/1337x-API).

Uses the API repo's adapter/fetcher (same selectors and magnet extraction)
but with a persistent browser session: the search-results page already
carries seeders and size, so only the winning torrent's detail page is
fetched for its magnet link. Results are appended to a JSONL cache, so the
run can be interrupted and resumed.

Selection rule: among results with size <= --max-size and seeders >=
--min-seeders, take the one with the most seeders.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.parse
from pathlib import Path

REPO = Path("/tmp/opencode/1337x-API")
if not REPO.exists():
    print(f"error: 1337x-API repo not found at {REPO}", file=sys.stderr)
    raise SystemExit(2)
sys.path.insert(0, str(REPO))

from bs4 import BeautifulSoup  # noqa: E402
from core.fetcher import logger as fetcher_logger  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from sites.one337x.adapter import One337xAdapter  # noqa: E402

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

SIZE_UNITS = {"": 1, "B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}

REQUEST_INTERVAL = 2.0  # seconds between page fetches; 1337x rate-limits hard


def size_to_bytes(text: str) -> int | None:
    match = re.search(r"([\d.]+)\s*([KMGT]?)i?B", text, re.IGNORECASE)
    if not match:
        return None
    return int(float(match.group(1)) * SIZE_UNITS.get(match.group(2).upper(), 1))


def to_int(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def read_movies(path: Path) -> list[dict[str, object]]:
    movies = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.match(r"^(\d+)\.\s+(.+)$", line)
        if not match:
            continue
        title = match.group(2).strip()
        year_match = re.search(r"\((\d{4})\)\s*$", title)
        year = year_match.group(1) if year_match else ""
        movies.append(
            {"rank": int(match.group(1)), "title": title, "year": year}
        )
    return movies


class TorrentScraper:
    """Thin wrapper around the 1337x-API adapter with one shared browser."""

    def __init__(self) -> None:
        self.adapter = One337xAdapter()
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=True)
        self.context = self.browser.new_context(user_agent=USER_AGENT)
        self.page = self.context.new_page()
        fetcher_logger.setLevel("WARNING")  # quiet per-URL INFO spam

    def close(self) -> None:
        self.context.close()
        self.browser.close()
        self.playwright.stop()

    def fetch_soup(
        self, url: str, retries: int = 3, wait_idle: bool = True
    ) -> BeautifulSoup | None:
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                time.sleep(REQUEST_INTERVAL)
                self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                if wait_idle:
                    self.page.wait_for_load_state("networkidle", timeout=15_000)
                soup = BeautifulSoup(self.page.content(), "html.parser")
                if self._is_challenge(soup):
                    raise RuntimeError("challenge / block page served")
                return soup
            except Exception as error:
                last_error = error
                wait = 10 * (attempt + 1)
                print(
                    f"  ! fetch attempt {attempt + 1} failed ({str(error)[:80]}); "
                    f"backing off {wait}s",
                    file=sys.stderr,
                )
                time.sleep(wait)
        print(f"  ! giving up on {url}: {last_error}", file=sys.stderr)
        return None

    @staticmethod
    def _is_challenge(soup: BeautifulSoup) -> bool:
        title = (soup.title.string or "").lower() if soup.title else ""
        markers = ("just a moment", "attention required", "access denied", "429")
        return any(m in title for m in markers)

    def search_rows(self, query: str, category: str, page: int) -> list[dict]:
        url = self.adapter.build_search_url(query, category, page)
        soup = self.fetch_soup(url)
        if soup is None:
            return []
        return self.adapter.scrape_search_page(soup, lambda u: self._detail(u), USER_AGENT)

    def _detail(self, url: str) -> BeautifulSoup:
        soup = self.fetch_soup(url)
        if soup is None:
            raise RuntimeError(f"detail fetch failed: {url}")
        return soup

    def search_rows_light(self, query: str, category: str, page: int) -> list[dict]:
        """Search rows without fetching any detail page (fast path)."""
        url = self.adapter.build_search_url(query, category, page)
        soup = self.fetch_soup(url)
        if soup is None:
            return []
        table = soup.find(
            "table", class_="table-list table table-responsive table-striped"
        )
        rows = []
        if table is None:
            return rows
        for tr in table.find_all("tr")[1:]:
            cols = tr.find_all("td")
            if len(cols) < 5:
                continue
            links = cols[0].find_all("a")
            if len(links) < 2 or not links[1].get("href"):
                continue
            rows.append(
                {
                    "name": cols[0].text.strip(),
                    "href": self.adapter.BASE_URL + links[1]["href"],
                    "Seeders": cols[1].text.strip(),
                    "Leechers": cols[2].text.strip(),
                    "Date": cols[3].text.strip(),
                    "Size": cols[4].text.strip(),
                }
            )
        return rows

    def find_matching_rows(
        self, title: str, year: str, category: str, max_pages: int
    ) -> list[dict]:
        """Search 1337x for a movie, escalating the query when needed.

        1337x OR-matches multi-word queries (so '12 Angry Men' also matches
        'Men in Black'), which buries the real movie. Strategy: try the full
        title once, then the most distinctive single words (longest first),
        scanning pages in site order and stopping at the first strict match.
        """
        def has_match(rows: list[dict]) -> bool:
            return any(
                name_matches(r.get("name", ""), title, year, strict=True)
                for r in rows
            )

        title_words = [
            w for w in normalize(title).split()
            if w != year and w not in {"the", "and", "for", "not"}
        ]
        # The full query keeps the title's word order and keeps numbers
        # ("12", "5", "7") that distinguish films. Single-word fallbacks use
        # only distinctive alphabetic words, longest first.
        primary = [w for w in title_words if len(w) >= 3 or w.isdigit()]
        words = sorted(
            {w for w in title_words if len(w) >= 3 and not w.isdigit()},
            key=len,
            reverse=True,
        )
        attempts = [(" ".join(primary), 1)] + [(w, max_pages) for w in words]
        seen: set[str] = set()
        for query, pages in attempts:
            for page in range(1, pages + 1):
                rows = self.search_rows_light(
                    urllib.parse.quote(query), category, page
                )
                if not rows:
                    break  # empty page: end of results (or rate limit)
                fresh = [r for r in rows if r["href"] not in seen]
                seen.update(r["href"] for r in fresh)
                if has_match(fresh):
                    return fresh
        return []

    def tpb_search(self, query: str, page: int = 0) -> list[dict]:
        """Pirate Bay search: magnets come straight from the results page.

        Fallback for movies whose 1337x search gets buried by the site's
        OR-matching (e.g. 'Black Swan' -> 'Black Panther'). Uses the
        pirate_bay adapter's selectors from the 1337x-API repo.
        """
        from sites.pirate_bay.adapter import PirateBayAdapter

        url = (
            f"{PirateBayAdapter.BASE_URL}/search.php?"
            f"q={urllib.parse.quote(query)}&search=Pirate+Search"
            f"&page={page}&orderby=7"  # orderby=7: sort by seeders
        )
        soup = self.fetch_soup(url, wait_idle=False)
        if soup is None:
            return []
        rows = []
        for entry in soup.find_all("li", class_="list-entry"):
            name_tag = entry.find("span", class_="item-title")
            seed_tag = entry.find("span", class_="item-seed")
            size_tag = entry.find("span", class_="item-size")
            magnet_tag = entry.find(
                "a", href=lambda h: h and h.startswith("magnet:")
            )
            if not (name_tag and seed_tag and size_tag and magnet_tag):
                continue
            # torrent-page link (for checking the posting before downloading)
            page_tag = name_tag.find("a", href=True)
            page_url = (
                PirateBayAdapter.BASE_URL + page_tag["href"]
                if page_tag else "Na"
            )
            rows.append(
                {
                    "name": name_tag.text.strip(),
                    "Seeders": seed_tag.text.strip(),
                    "Size": size_tag.get_text().strip(),
                    "magnet": magnet_tag["href"],
                    "source": "tpb",
                    "page_url": page_url,
                }
            )
        return rows

    def magnet_for(self, detail_url: str) -> str:
        soup = self.fetch_soup(detail_url, wait_idle=False)
        if soup is None:
            return "Na"
        tag = soup.find(
            lambda t: t.name == "a" and "Magnet Download" in t.get_text(strip=True)
        )
        return tag.get("href") if tag and tag.has_attr("href") else "Na"


def normalize(text: str) -> str:
    """Lowercase, fold accents, and reduce punctuation to single spaces.

    Apostrophes are dropped rather than spaced so "Cuckoo's" stays one word;
    separators such as &, :, + and · become spaces. Accents are folded first,
    so "Cléo" becomes "cleo" instead of splitting into "cl o".
    """
    text = text.replace("\\", "").replace("'", "")
    text = text.replace("&", " and ")
    text = re.sub(r"[·•–—:+/]", " ", text)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def name_matches(torrent_name: str, title: str, year: str, strict: bool) -> bool:
    """Require the torrent name to look like the actual movie.

    The title's words must occur in the torrent name as an *ordered
    subsequence*, which preserves word order and repeated words. The old
    set-based comparison let "Sing Sing" match a name containing a single
    "sing". Strict mode additionally requires the title to be tightly packed
    and either distinctive (>=3 words) or near the front of the release name,
    so a bare "Monster" no longer matches "...and Her Monster 2023" and
    "The Return 2003" no longer matches "...The Return of the King 2003".
    """
    name_tokens = normalize(torrent_name).split()
    if not name_tokens:
        return False
    if year and year not in set(name_tokens):
        return False

    title_no_year = re.sub(r"\s*\(\d{4}\)\s*$", "", title)
    wanted = [t for t in normalize(title_no_year).split() if t != year]
    if not wanted:
        # Title normalizes to nothing (pure punctuation); nothing to match.
        return True

    position = 0
    start = last = None
    for token in wanted:
        try:
            index = name_tokens.index(token, position)
        except ValueError:
            return False
        if start is None:
            start = index
        position = index + 1
        last = index

    if not strict:
        return True
    extra = (last - start + 1) - len(wanted)
    return extra <= 2 and (len(wanted) >= 3 or start <= 3)


def pick_best(
    rows: list[dict], max_size: int, min_seeders: int, title: str, year: str,
    strict_only: bool = False,
) -> tuple[dict | None, str]:
    def collect(strict: bool) -> list[dict]:
        found = []
        for row in rows:
            size_bytes = size_to_bytes(row.get("Size", ""))
            seeders = to_int(row.get("Seeders", ""))
            if (
                size_bytes is None or seeders is None
                or size_bytes > max_size or seeders < min_seeders
            ):
                continue
            if not name_matches(row.get("name", ""), title, year, strict=strict):
                continue
            found.append(dict(row, size_bytes=size_bytes, seeders=seeders))
        return found

    eligible = collect(strict=True)
    if not eligible and not strict_only:
        # relaxed: year + at least half the title words
        eligible = collect(strict=False)
    if not eligible:
        return None, "no torrent matching title within size/seeders limits"
    eligible.sort(key=lambda r: (r["seeders"], r["size_bytes"]), reverse=True)
    return eligible[0], "ok"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("movie_list", type=Path, help="numbered movie list file")
    parser.add_argument(
        "--category", default="Movies", help="1337x category (default: Movies)"
    )
    parser.add_argument(
        "--max-size-gb", type=float, default=4.0, help="max torrent size in GB"
    )
    parser.add_argument(
        "--min-seeders", type=int, default=10, help="minimum seeders required"
    )
    parser.add_argument(
        "--max-pages", type=int, default=5,
        help="search result pages to scan per movie (default: 5)",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="only process first N movies (0 = all)"
    )
    parser.add_argument(
        "--cache", type=Path, default=Path("torrents-cache.jsonl"),
        help="JSONL cache file for resume (default: torrents-cache.jsonl)",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("torrents.json"),
        help="final JSON output file (default: torrents.json)",
    )
    parser.add_argument(
        "--retry-failed", action="store_true",
        help="re-fetch cached movies whose status is not 'ok'",
    )
    parser.add_argument(
        "--only", default="",
        help="comma-separated ranks to process (e.g. 104,119)",
    )
    parser.add_argument(
        "--tpb-only", action="store_true",
        help="skip 1337x (Cloudflare-blocked) and search Pirate Bay only",
    )
    parser.add_argument(
        "--overrides", type=Path, default=Path("query-overrides.json"),
        help="JSON map of rank -> alternate/original-language query "
             "(default: query-overrides.json)",
    )
    args = parser.parse_args()

    movies = read_movies(args.movie_list)
    if args.only:
        wanted = {int(r) for r in args.only.split(",") if r.strip()}
        movies = [m for m in movies if m["rank"] in wanted]
    if args.limit:
        movies = movies[: args.limit]
    if not movies:
        print("error: no movies parsed from list", file=sys.stderr)
        return 2

    max_size = int(args.max_size_gb * 1024**3)
    done: dict[int, dict] = {}
    if args.cache.exists():
        for line in args.cache.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and "rank" in record:
                done[record["rank"]] = record

    if args.retry_failed:
        pending = [
            m for m in movies
            if m["rank"] not in done or done[m["rank"]].get("status") != "ok"
        ]
    else:
        pending = [m for m in movies if m["rank"] not in done]
    print(
        f"{len(movies)} movies, {len(done)} already cached, {len(pending)} to go "
        f"(max {args.max_size_gb:g} GB, min {args.min_seeders} seeders)",
        flush=True,
    )

    overrides: dict[str, str] = {}
    if args.overrides.exists():
        try:
            overrides = json.loads(args.overrides.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"warning: ignoring malformed {args.overrides}", file=sys.stderr)

    scraper = TorrentScraper()
    try:
        with args.cache.open("a", encoding="utf-8") as cache:
            for movie in pending:
                query = movie["title"]
                query = urllib.parse.quote(query, safe="")
                started = time.monotonic()
                # An alternate/original-language title (e.g. "Los Olvidados"
                # for "The Young and the Damned") is matched against itself,
                # since the torrent will not contain the English words.
                alternate = overrides.get(str(movie["rank"]), "").strip()
                if args.tpb_only or alternate:
                    rows: list[dict] = []
                    best, reason = None, "no torrent matching title within size/seeders limits"
                else:
                    rows = scraper.find_matching_rows(
                        movie["title"], movie["year"], args.category, args.max_pages
                    )
                    best, reason = pick_best(
                        rows, max_size, args.min_seeders,
                        movie["title"], movie["year"],
                    )

                if best is None:
                    # 1337x came up empty (or was skipped): fall back to
                    # Pirate Bay, whose search matches titles exactly and
                    # carries the magnet directly (no detail-page visit
                    # needed). Try several query shapes: full title, title
                    # without filler words ("the", "movie", ...), each with
                    # and without the year.
                    year = movie["year"]
                    bare = " ".join(
                        normalize(
                            re.sub(r"\s*\(\d{4}\)\s*$", "", movie["title"])
                        ).split()
                    )
                    filler = {
                        "the", "a", "an", "of", "and", "movie", "part",
                        "from", "to", "no", "le", "la", "les", "il", "di",
                        "da", "du", "de", "d", "o", "e", "iii", "ii",
                    }
                    core = " ".join(
                        w for w in bare.split() if w not in filler
                    )
                    variants: list[tuple[str, str]] = []
                    if alternate:
                        variants.append(
                            (f"{alternate} {year}".strip(), alternate)
                        )
                    for q in (bare, core):
                        if q:
                            if year:
                                variants.append((f"{q} {year}", movie["title"]))
                            variants.append((q, movie["title"]))
                    for query, match_title in variants:
                        tpb_rows = scraper.tpb_search(query)
                        best, reason = pick_best(
                            tpb_rows, max_size, args.min_seeders,
                            match_title, year,
                        )
                        if best is not None:
                            break

                record = {
                    "rank": movie["rank"],
                    "title": movie["title"],
                    "query": urllib.parse.unquote(query),
                    "status": reason,
                    "magnet": "Na",
                    "torrent_name": None,
                    "seeders": None,
                    "size": None,
                    "candidates": len(rows),
                }
                if best is not None:
                    magnet = best.get("magnet", "Na")
                    if magnet in ("Na", "", None):
                        magnet = scraper.magnet_for(best["href"])
                    record.update(
                        {
                            "status": "ok" if magnet != "Na" else "magnet not found",
                            "magnet": magnet,
                            "torrent_name": best["name"],
                            "seeders": best["seeders"],
                            "size": best["Size"],
                            "source": best.get("source", "1337x"),
                            "page_url": best.get("page_url")
                            or best.get("href", "Na"),
                        }
                    )
                done[record["rank"]] = record
                cache.write(json.dumps(record, ensure_ascii=False) + "\n")
                cache.flush()
                print(
                    f"{record['rank']:>3}. {movie['title']} -> "
                    f"{record['status']}"
                    + (f" ({record['seeders']} seeders, {record['size']})"
                       if record["seeders"] is not None else "")
                    + f" [{time.monotonic() - started:.1f}s]",
                    flush=True,
                )
                time.sleep(0.5)
    finally:
        scraper.close()

    results = [done[m["rank"]] for m in read_movies(args.movie_list) if m["rank"] in done]
    # migrate older cache records that stored the 1337x page as detail_url
    for r in results:
        r.setdefault("page_url", r.get("detail_url") or "Na")
    args.output.write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    magnets_path = args.output.with_name("magnets.txt")
    with magnets_path.open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(f"{r['rank']}. {r['title']}\n")
            if r.get("torrent_name"):
                fh.write(
                    f"   {r['torrent_name']} | {r['seeders']} seeders | {r['size']}\n"
                )
            page = r.get("page_url")
            if page and page != "Na":
                fh.write(f"   page: {page}\n")
            fh.write(f"   {r['magnet']}\n\n")
    ok = sum(1 for r in results if r["magnet"] != "Na" and r["status"] == "ok")
    print(
        f"Wrote {len(results)} results to {args.output} ({ok} with magnets); "
        f"magnets list: {magnets_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
