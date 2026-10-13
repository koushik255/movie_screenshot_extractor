#!/usr/bin/env python3
"""Download torrents -> ffmpeg screenshots -> GPU nvJPEG embeddings.

Runs on the GPU box. For each movie from torrents.json:

  1. aria2c downloads the magnet (skipped when a video is already staged)
  2. ffmpeg writes screenshots/<slug>/frame_%06d.jpg  (2 fps, <=1280 px, q5)
  3. Curtain's CudaIndexer decodes those JPEGs with nvJPEG on the GPU and runs
     best.pt to write indexes/<slug>/{embeddings.npy,records.json,manifest.json}
  4. the movie file is deleted and the rank is appended to the state file
     with status "done" (or "failed"); re-runs skip done ranks

The index schema matches apps/server.py: every manifest carries
checkpoint_sha256 + source_sha256, and a collection.json is written at the end.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/gpu"))
from curtain_ml import index as curtain_index  # noqa: E402
from curtain_ml.index import CudaIndexer  # noqa: E402

from common import (  # noqa: E402
    BT_TRACKER_FLAG, append_state, find_video, frame_name, frames_complete,
    load_ranks, saved_frames, seek_arg,
)

SIZE_UNITS = {"": 1, "B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def slugify(title: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")


def size_bytes(text: object) -> int:
    match = re.search(r"([\d.]+)\s*([KMGT]?)i?B", str(text), re.IGNORECASE)
    if not match:
        return 0
    return int(float(match.group(1)) * SIZE_UNITS.get(match.group(2).upper(), 1))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(entry: dict, staging: Path, attempts: int = 2) -> Path:
    rank = entry["rank"]
    target = staging / str(rank)
    # A complete staged download has no .aria2 control file next to it.
    if target.exists() and not any(target.rglob("*.aria2")):
        staged = find_video(target)
        if staged is not None:
            print(f"  already staged: {staged.name}", flush=True)
            return staged
    target.mkdir(parents=True, exist_ok=True)
    # A .torrent saved by probe_torrents.py starts aria2c from known-good
    # metadata, skipping the metadata handshake that stalls slow swarms.
    probed = entry.get("probe_torrent")
    source = (
        probed if probed and os.path.isfile(probed)
        else entry.get("torrent_file") or entry["magnet"]
    )
    command = [
        "aria2c", "--seed-time=0", "--file-allocation=none",
        "--console-log-level=warn", "--summary-interval=10",
        # --bt-stop-timeout is disabled: metadata exchange reports DL:0B and
        # would be killed as a stall. The progress-aware loop below decides.
        "--bt-stop-timeout=0", "--check-integrity=true",
        "--allow-overwrite=true", "--continue=true",
        "--enable-dht=true",
        "--dht-entry-point=router.bittorrent.com:6881",
        "--dht-entry-point=dht.transmissionbt.com:6881",
        BT_TRACKER_FLAG,
        "-d", str(target), source,
    ]
    # Abandon a torrent that makes no progress for STALL_LIMIT, so one dead
    # swarm cannot block the whole batch. Progress is a *change* in the
    # reported percentage (which advances during metadata too) or a DL rate
    # at/above MIN_DL_SPEED; aria2 repeats the same percentage when stalled.
    MIN_DL_SPEED = 50 * 1024
    STALL_LIMIT = 300.0
    OVERALL_LIMIT = 2400.0
    speed_units = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3}
    last = None
    for attempt in range(attempts):
        started = time.monotonic()
        last_progress = started
        last_percent = -1
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        assert process.stdout is not None
        stalled = False
        for line in process.stdout:
            now = time.monotonic()
            speed = re.search(r"DL:([\d.]+)([KMG]?)i?B", line)
            if (
                speed
                and float(speed.group(1)) * speed_units[speed.group(2)]
                >= MIN_DL_SPEED
            ):
                last_progress = now
            percent = re.search(r"\((\d+)%\)", line)
            if percent and int(percent.group(1)) != last_percent:
                last_percent = int(percent.group(1))
                last_progress = now
            if now - last_progress > STALL_LIMIT or now - started > OVERALL_LIMIT:
                stalled = True
                process.terminate()
                process.wait()
                break
        if not stalled:
            process.wait()
        controls = list(target.rglob("*.aria2"))
        video = find_video(target)
        if stalled:
            last = (
                f"no progress for >{STALL_LIMIT / 60:.0f} min or over the "
                f"{OVERALL_LIMIT / 60:.0f} min cap"
            )
        elif process.returncode != 0:
            last = f"aria2c exit {process.returncode}"
        elif controls:
            last = f"{len(controls)} aria2 control file(s) left"
        elif video is None:
            last = "no video file found"
        else:
            return video
        print(f"  download attempt {attempt + 1} failed ({last}); retrying",
              flush=True)
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
    raise RuntimeError(last or "download failed")


def frame_total(video: Path, sample_fps: float) -> int:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(video)],
            capture_output=True, text=True, check=True,
        )
    except FileNotFoundError:
        raise RuntimeError("ffprobe not found; is ffmpeg installed?")
    except subprocess.CalledProcessError:
        raise RuntimeError(f"could not determine duration of {video}")
    total = int(float(result.stdout.strip()) * sample_fps + 0.999999)
    if total <= 0:
        raise RuntimeError(f"could not determine a frame count for {video}")
    return total


def extract_screenshots(video: Path, shot_dir: Path, sample_fps: float,
                        width: int, quality: int) -> int:
    """Extract frames, resuming an interrupted run instead of trusting a
    partial directory. The previous version reused any existing frame_*.jpg,
    which silently indexed a truncated frame set after a crash."""
    shot_dir.mkdir(parents=True, exist_ok=True)
    total = frame_total(video, sample_fps)
    saved = min(saved_frames(shot_dir), total)
    if saved >= total:
        print(f"  reusing {saved} existing screenshots", flush=True)
        return saved
    if saved > 0:
        # The last JPEG may be incomplete if a previous run was interrupted.
        (shot_dir / frame_name(saved)).unlink(missing_ok=True)
        saved -= 1

    filter_graph = f"fps={sample_fps},scale='min({width},iw)':-2"
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", seek_arg(saved / sample_fps),
        "-i", str(video),
        "-vf", filter_graph, "-q:v", str(quality),
        "-start_number", str(saved + 1),
        str(shot_dir / "frame_%06d.jpg"),
    ]
    subprocess.run(command, stdin=subprocess.DEVNULL, check=True)
    final = saved_frames(shot_dir)
    if not frames_complete(final, total, sample_fps):
        raise RuntimeError(
            f"expected {total} frames but found {final}; extraction incomplete"
        )
    return final


def write_collection(index_root: Path, checkpoint_sha256: str,
                     checkpoint_run: str) -> dict:
    movies = []
    for folder in sorted(index_root.iterdir()):
        manifest_path = folder / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("checkpoint_sha256") != checkpoint_sha256:
            continue
        if not (folder / "records.json").is_file():
            continue
        if not (folder / "embeddings.npy").is_file():
            continue
        movies.append(manifest)
    collection = {
        "checkpoint_sha256": checkpoint_sha256,
        "checkpoint_run": checkpoint_run,
        "movies": movies,
        "total_frames": sum(int(m.get("frames", 0)) for m in movies),
    }
    (index_root / "collection.json").write_text(
        json.dumps(collection, indent=2) + "\n", encoding="utf-8"
    )
    return collection


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--torrents", type=Path, default=Path.home() / "gpu/torrents.json")
    parser.add_argument("--checkpoint", type=Path, default=Path.home() / "gpu/best.pt")
    parser.add_argument("--root", type=Path, default=Path.home() / "gpu/lbfive_gpu")
    parser.add_argument("--collection", default="lbfive-gpu-crop-v1")
    parser.add_argument("--limit", type=int, default=0, help="process at most N (0 = all)")
    parser.add_argument("--only", default="", help="comma-separated ranks to process")
    parser.add_argument("--max-size-gb", type=float, default=4.0)
    parser.add_argument("--min-seeders", type=int, default=10)
    parser.add_argument("--sample-fps", type=float, default=2.0)
    parser.add_argument("--screenshot-width", type=int, default=1280)
    parser.add_argument("--jpeg-quality", type=int, default=5,
                        help="ffmpeg -q:v (lower = better, 2-31)")
    parser.add_argument("--file-workers", type=int, default=3,
                        help="DataLoader workers for nvJPEG indexing (RAM-bound)")
    parser.add_argument("--batch-size", type=int, default=256,
                        help="nvJPEG/model batch size")
    parser.add_argument("--keep-movies", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    curtain_index.FILE_WORKERS = args.file_workers
    curtain_index.BATCH_SIZE = args.batch_size

    staging = args.root / "staging"
    screenshots = args.root / "screenshots"
    indexes = args.root / "indexes"
    state = args.root / "pipeline-state.jsonl"
    for directory in (staging, screenshots, indexes):
        directory.mkdir(parents=True, exist_ok=True)

    records = json.loads(args.torrents.read_text(encoding="utf-8"))
    records.sort(key=lambda r: r.get("rank", 0))
    done = load_ranks(state, "done")
    only = {int(r) for r in args.only.split(",") if r.strip()} if args.only else None

    selected = []
    probe_dead = 0
    for record in records:
        rank = record.get("rank")
        if rank in done:
            continue
        if only is not None and rank not in only:
            continue
        if record.get("status") != "ok":
            continue
        # Probe annotations are only present when --torrents points at
        # torrents-probed.json; a magnet that reached no peers is skipped
        # rather than stalling a download slot.
        if record.get("probe_alive") is False:
            probe_dead += 1
            continue
        if not str(record.get("magnet", "")).startswith("magnet:"):
            continue
        if size_bytes(record.get("size", "")) > args.max_size_gb * 1024**3:
            continue
        if (record.get("seeders") or 0) < args.min_seeders:
            continue
        selected.append(record)
        if args.limit and len(selected) >= args.limit:
            break

    checkpoint_sha256 = sha256(args.checkpoint)
    checkpoint_run = args.checkpoint.parent.name
    print(
        f"{len(selected)} movie(s) selected | collection={args.collection} | "
        f"checkpoint={args.checkpoint.name} ({checkpoint_sha256[:12]})"
        + (f" | {probe_dead} dead magnet(s) skipped" if probe_dead else "")
    )
    if args.dry_run:
        for record in selected:
            print(f"  {record['rank']:>3}. {record['title']} ({record.get('size')})")
        return 0

    indexer = None
    failures = 0
    for position, record in enumerate(selected, 1):
        rank = record["rank"]
        title = record["title"]
        slug = slugify(title)
        video: Path | None = None
        print(f"\n[{position}/{len(selected)}] {rank}. {title}", flush=True)
        started = time.perf_counter()
        try:
            t0 = time.perf_counter()
            video = download(record, staging)
            download_seconds = time.perf_counter() - t0

            t0 = time.perf_counter()
            frames = extract_screenshots(
                video, screenshots / slug, args.sample_fps,
                args.screenshot_width, args.jpeg_quality,
            )
            screenshot_seconds = time.perf_counter() - t0
            if frames == 0:
                raise RuntimeError("no screenshots produced")

            if indexer is None:
                indexer = CudaIndexer(args.checkpoint)
            t0 = time.perf_counter()
            manifest = indexer.index_movie(screenshots / slug, indexes / slug)
            embed_seconds = time.perf_counter() - t0

            source_sha256 = sha256(video)
            manifest.update(
                checkpoint_sha256=checkpoint_sha256,
                checkpoint_run=checkpoint_run,
                source_sha256=source_sha256,
                source=str(video),
                source_size=video.stat().st_size,
                source_collection="lbfive",
                output_collection=args.collection,
                sample_fps=args.sample_fps,
                screenshots=str(screenshots / slug),
                download_seconds=round(download_seconds, 1),
                screenshot_seconds=round(screenshot_seconds, 1),
                embed_seconds=round(embed_seconds, 1),
            )
            (indexes / slug / "manifest.json").write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
            )
            append_state(state, {
                "rank": rank, "title": title, "status": "done",
                "frames": manifest["frames"], "slug": slug,
            })
            write_collection(indexes, checkpoint_sha256, checkpoint_run)
            print(
                f"  done: {manifest['frames']} frames | "
                f"download {download_seconds:.0f}s screenshot {screenshot_seconds:.0f}s "
                f"embed {embed_seconds:.1f}s | total {time.perf_counter()-started:.0f}s",
                flush=True,
            )
        except Exception as error:  # noqa: BLE001 - keep the batch going
            failures += 1
            append_state(state, {
                "rank": rank, "title": title, "status": "failed",
                "reason": str(error)[:200],
            })
            print(f"  FAILED: {error}", file=sys.stderr, flush=True)
        finally:
            if not args.keep_movies:
                shutil.rmtree(staging / str(rank), ignore_errors=True)

    collection = write_collection(indexes, checkpoint_sha256, checkpoint_run)
    print(
        f"\ncollection {args.collection}: {len(collection['movies'])} movies, "
        f"{collection['total_frames']} frames | {failures} failed this run"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
