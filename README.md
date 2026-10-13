# Letterboxd movie-list extractor

Save the page source as an HTML file, then run:

```bash
python3 extract_movies.py page-source.html > movies.txt
```

Multiple paginated source files can be merged in one command; entries are
sorted by their displayed rank:

```bash
python3 extract_movies.py first100.html 200.html 300.html 400.html 500.html \
  --output movies.txt
```

The default output is a numbered title list:

```text
1. Harakiri (1962)
2. The Human Condition III: A Soldier's Prayer (1961)
3. 12 Angry Men (1957)
```

CSV and JSON output are also available:

```bash
python3 extract_movies.py page-source.html --format csv --output movies.csv
python3 extract_movies.py page-source.html --format json --output movies.json
```

You can pipe or paste HTML through standard input by omitting the filename:

```bash
python3 extract_movies.py < page-source.html
```

The parser uses only Python's standard library. It selects every `<ul>` whose
classes include `poster-list -p125 -grid`, then extracts each item's displayed
name, rank, slug, and Letterboxd-relative link.

It also detects and unwraps files saved directly from Chromium's styled
`view-source:` page (the format containing `td.line-content` elements).

## Checking torrents are alive before downloading

`pipeline.py` and `gpu_pipeline.py` waste a download slot (up to a full stall
timeout) on a magnet whose swarm is unreachable. Probe first:

```bash
python3 probe_torrents.py            # probes uncached magnets, 8 at a time
```

A magnet is *alive* when aria2c's metadata-only mode saves its
`<infohash>.torrent`; a few hundred KB of metadata is enough to prove a peer is
reachable. Results are cached per info-hash in `probe-cache.jsonl` (re-runs are
free; `--refresh` re-checks) and written back as `torrents-probed.json` with
`probe_alive` / `probe_status` / `probe_seeds` / `probe_connections` per record.
Saved metadata goes to `probe-torrents/`.

When `torrents-probed.json` exists, `pipeline.py` reads it instead of
`torrents.json`, skips records whose `probe_alive` is `false`, and feeds the
saved `.torrent` to aria2c so the download starts from known-good metadata.
`probe_status` is one of:

- `alive` — metadata received; safe to download.
- `unconfirmed` — peers connected but metadata did not arrive in time; still
  processed (never skipped), since a slow swarm is not a dead one.
- `dead` — no peers at all within the timeout; skipped unless `--refresh`
  later proves otherwise.

Useful flags: `--jobs N`, `--timeout SECONDS` (default 45), `--limit N`,
`--refresh`, `--dry-run`.

Both downloaders run aria2c with `--bt-stop-timeout=0`: metadata exchange
reports `DL:0B`, so aria2's stall timeout used to kill live-but-slow swarms
mid-handshake (the source of most `aria2c failed (exit 7)` results). A
progress-aware stall detector (5 minutes without a percentage change or a
rate above 50 KB/s, and a 1 hour cap) is the real deadline, and
`--check-integrity` verifies piece hashes before ffmpeg sees a file.

## Re-fetching movies that found no torrent

Search matching now folds accents (`Cléo` -> `cleo`), keeps numbers (`12`,
`5`), and requires the title's words as an *ordered subsequence* near the
front of the release name, so wrong matches such as `M` -> *City Lights* or
`Monster` -> *...and Her Monster* are rejected. To retry cached failures:

```bash
python3 fetch_torrents.py movies-500.txt --retry-failed
```

1337x now serves a Cloudflare "Just a moment…" challenge to headless Chromium
on every mirror, so the 1337x search hangs in retry/backoff. Use
`--tpb-only` to go straight to Pirate Bay, which still works and carries
magnets on the results page:

```bash
python3 fetch_torrents.py movies-500.txt --retry-failed --tpb-only
```

Other flags: `--only 104,119` limits to specific ranks, `--max-size-gb N`
raises the size cap (older 1080p arthouse releases often exceed 4 GB), and
`--overrides query-overrides.json` maps a rank to an alternate/original-language
title that is matched against itself, e.g. `"300": "Los Olvidados"` for
*The Young and the Damned*.

With `--min-seeders 4 --max-size-gb 8`, 499 of the 500 titles now have a
magnet. The one holdout, `Princes and Princesses (2000)`, has no release on
Pirate Bay under any title. Five titles needed `query-overrides.json`
(alternate/original-language titles), and several older 1080p releases are
5-8 GB, so run the GPU pipeline with `--max-size-gb 8` to include them.

# movie_screenshot_extractor
