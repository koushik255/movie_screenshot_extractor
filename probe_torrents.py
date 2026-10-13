#!/usr/bin/env python3
"""Probe every magnet in torrents.json for liveness before downloading.

A magnet is just an info-hash, so "is it alive?" can only be answered by
joining the swarm. We do that cheaply with aria2c's metadata-only mode: fetch
the torrent metadata (a few hundred KB) instead of the multi-GB movie. A
reachable peer must exist to hand over metadata, so a saved `<infohash>.torrent`
is a reliable liveness signal. aria2c's progress readout also yields `CN`
(connected peers) and `SD` (seeders); `CN` is a rough swarm-size hint, but the
scraped `seeders` field is still the better ranking signal because `SD` reads
low during a metadata-only transfer.

Probes run in parallel and are cached per info-hash in probe-cache.jsonl, so
re-runs are free; pass --refresh to re-check (e.g. when a swarm revives).

Writes torrents-probed.json: every input record plus `probe_alive`,
`probe_status`, `probe_seeds`, `probe_connections`, `probe_elapsed`,
`probe_checked`. The saved metadata is kept in
probe-torrents/<infohash>.torrent (a ready-made local .torrent that a download
can use to skip metadata resolution). pipeline.py skips records whose `probe_alive` is false.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from common import BT_TRACKER_FLAG

INFO_HASH_RE = re.compile(r"xt=urn:btih:([0-9a-zA-Z]{32,40})")


def infohash(magnet: str) -> str | None:
    """Return the v1 info-hash as lowercase hex, accepting base32 magnets."""
    match = INFO_HASH_RE.search(magnet)
    if not match:
        return None
    raw = match.group(1)
    if len(raw) == 32:  # base32-encoded info-hash
        try:
            raw = base64.b32decode(raw.upper()).hex()
        except Exception:  # noqa: BLE001 - malformed magnet
            return None
    return raw.lower()


def cache_key(magnet: str) -> str:
    return infohash(magnet) or "sha1:" + hashlib.sha1(
        magnet.encode("utf-8")
    ).hexdigest()


def probe(
    magnet: str, probe_dir: Path, timeout: float
) -> dict[str, object]:
    """Run one metadata-only aria2c probe and return its result fields."""
    target = probe_dir / cache_key(magnet)
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)

    command = [
        "aria2c",
        "--seed-time=0",
        "--file-allocation=none",
        "--console-log-level=warn",
        "--summary-interval=1",
        "--bt-metadata-only=true",
        "--bt-save-metadata=true",
        # Disable aria2's stall timeout: metadata exchange reports DL:0B, so
        # bt-stop-timeout kills slow-but-alive swarms mid-handshake. The
        # wall-clock kill below is the only deadline.
        "--bt-stop-timeout=0",
        "--enable-dht=true",
        "--dht-entry-point=router.bittorrent.com:6881",
        BT_TRACKER_FLAG,
        "-d", str(target),
        magnet,
    ]

    started = time.monotonic()
    output = ""
    timed_out = False
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
    except FileNotFoundError:
        return {
            "alive": None,
            "status": "error",
            "error": "aria2c not found",
            "elapsed": 0.0,
        }
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        output, _ = process.communicate()
    elapsed = time.monotonic() - started

    metadata = list(target.glob("*.torrent"))
    connections = [int(n) for n in re.findall(r"CN:(\d+)", output)]
    seeds = [int(n) for n in re.findall(r"SD:(\d+)", output)]
    max_connections = max(connections, default=0)

    # Three-way verdict. Metadata means definitely alive. No metadata but peers
    # connected means slow/inconclusive - do not skip it. Only a torrent that
    # reached nobody at all is called dead, so a false "dead" needs zero
    # connections, not merely a slow handshake.
    if metadata:
        alive: bool | None = True
        status = "alive"
    elif max_connections > 0:
        alive = None
        status = "unconfirmed"
    else:
        alive = False
        status = "dead"

    result: dict[str, object] = {
        "alive": alive,
        "status": status,
        "seeds": max(seeds, default=0),
        "connections": max_connections,
        "elapsed": round(elapsed, 1),
    }
    if metadata:
        result["torrent_file"] = str(metadata[0])
    elif timed_out and max_connections == 0:
        result["error"] = "probe timed out with no peers"
    return result


def load_cache(path: Path) -> dict[str, dict]:
    cache: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = record.get("key")
            if key:
                cache[key] = record
    return cache


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--torrents", type=Path, default=Path("torrents.json"),
        help="input records (default: torrents.json)",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("torrents-probed.json"),
        help="records + probe fields (default: torrents-probed.json)",
    )
    parser.add_argument(
        "--cache", type=Path, default=Path("probe-cache.jsonl"),
        help="per-info-hash cache (default: probe-cache.jsonl)",
    )
    parser.add_argument(
        "--probe-dir", type=Path, default=Path("probe-torrents"),
        help="where saved .torrent metadata is kept (default: probe-torrents)",
    )
    parser.add_argument(
        "--jobs", type=int, default=8, help="parallel probes (default: 8)"
    )
    parser.add_argument(
        "--timeout", type=float, default=45.0,
        help="seconds a probe waits for metadata (default: 45)",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="probe at most N records (0 = all)"
    )
    parser.add_argument(
        "--refresh", action="store_true",
        help="re-probe even when a cached result exists",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="list what would be probed without running aria2c",
    )
    args = parser.parse_args()

    if not args.torrents.exists():
        print(f"error: {args.torrents} not found", file=sys.stderr)
        return 2
    records = json.loads(args.torrents.read_text(encoding="utf-8"))

    cache = load_cache(args.cache)
    targets: dict[str, str] = {}  # key -> magnet, insertion order preserved
    missing = 0
    for record in records:
        magnet = str(record.get("magnet") or "")
        if not magnet.startswith("magnet:"):
            missing += 1
            continue
        key = cache_key(magnet)
        cached = cache.get(key)
        # Only completed verdicts are cached; environment errors are retried.
        if (
            not args.refresh
            and cached
            and cached.get("status") in ("alive", "unconfirmed", "dead")
        ):
            continue
        targets.setdefault(key, magnet)
        if args.limit and len(targets) >= args.limit:
            break

    print(
        f"{len(records)} records | {len(cache)} cached | "
        f"{len(targets)} to probe | {missing} without magnet",
        flush=True,
    )
    if args.dry_run:
        for key, magnet in targets.items():
            print(f"  {key}  {magnet[:90]}")
        return 0
    if not targets:
        print("nothing to probe; writing output from cache")
    else:
        args.probe_dir.mkdir(parents=True, exist_ok=True)
        done = 0
        alive = 0
        started = time.monotonic()
        # Append to the cache as results land so an interrupted run resumes.
        with args.cache.open("a", encoding="utf-8") as cache_file:
            with ThreadPoolExecutor(max_workers=args.jobs) as pool:
                futures = {
                    pool.submit(probe, magnet, args.probe_dir, args.timeout): key
                    for key, magnet in targets.items()
                }
                for future in as_completed(futures):
                    key = futures[future]
                    outcome = future.result()
                    done += 1
                    if outcome.get("alive"):
                        alive += 1
                    record = {
                        "key": key,
                        "magnet": targets[key],
                        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        **outcome,
                    }
                    cache[key] = record
                    cache_file.write(
                        json.dumps(record, ensure_ascii=False) + "\n"
                    )
                    cache_file.flush()
                    print(
                        f"  [{done}/{len(targets)}] {key[:12]} "
                        f"{outcome.get('status')} "
                        f"seeds={outcome.get('seeds', 0)} "
                        f"conns={outcome.get('connections', 0)} "
                        f"{outcome.get('elapsed', 0):.1f}s",
                        flush=True,
                    )
        print(
            f"probed {alive}/{done} alive in {time.monotonic() - started:.0f}s",
            flush=True,
        )

    dead_ranks: list[int] = []
    unknown = 0
    for record in records:
        magnet = str(record.get("magnet") or "")
        if not magnet.startswith("magnet:"):
            continue
        cached = cache.get(cache_key(magnet))
        if not cached:
            unknown += 1
            continue
        record["probe_alive"] = cached.get("alive")
        record["probe_status"] = cached.get("status")
        record["probe_seeds"] = cached.get("seeds", 0)
        record["probe_connections"] = cached.get("connections", 0)
        record["probe_elapsed"] = cached.get("elapsed")
        record["probe_checked"] = cached.get("checked_at")
        if cached.get("torrent_file"):
            record["probe_torrent"] = cached["torrent_file"]
        if cached.get("alive") is False:
            dead_ranks.append(record["rank"])

    alive_total = sum(1 for r in records if r.get("probe_alive") is True)
    dead_total = sum(1 for r in records if r.get("probe_alive") is False)
    unconfirmed = sum(1 for r in records if r.get("probe_status") == "unconfirmed")
    args.output.write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"\n{alive_total} alive, {dead_total} dead, "
        f"{unconfirmed} unconfirmed, {unknown} unprobed | "
        f"wrote {args.output}",
        file=sys.stderr,
    )
    if dead_ranks:
        preview = ", ".join(str(r) for r in dead_ranks[:20])
        more = "" if len(dead_ranks) <= 20 else f" (+{len(dead_ranks) - 20} more)"
        print(f"dead ranks: {preview}{more}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
