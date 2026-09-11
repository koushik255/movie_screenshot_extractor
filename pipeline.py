#!/usr/bin/env python3
"""Download movies from torrents and extract screenshot frames, in sequence.

Pipeline (producer/consumer): two aria2c download slots feed a pool of two
frame-extraction workers. For each movie: download the torrent with
--seed-time=0, pick the largest video file in the download directory, extract
frames at 2 fps (same logic as screenshots.py, resumable), append a line to
completed.txt (rank, title, torrent page URL, frame count), then delete the
downloaded files to reclaim disk.

State lives in pipeline-state.jsonl so an interrupted run skips movies whose
screenshots are already done. Output is a single compact status line.
"""

from __future__ import annotations

import json
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from screenshots import FPS, VIDEO_FILTER, frame_name, frame_total, saved_frames

TORRENTS_JSON = Path("torrents.json")
STATE_FILE = Path("pipeline-state.jsonl")
COMPLETED_LOG = Path("completed.txt")
DOWNLOAD_ROOT = Path("movies")
SCREENSHOT_ROOT = Path("screenshots")

DOWNLOAD_JOBS = 2
EXTRACT_JOBS = 2

# aria2c: stop seeding immediately after download; give up on a stalled
# torrent after 120s without data so a dead slot can move on. Fresh
# trackers + a DHT entry point are needed because magnets scraped from
# 1337x carry stale tracker lists (dead coppersurfer/rarbg hosts), which
# stalls metadata resolution with exit 7.
FRESH_TRACKERS = [
    "udp://zer0day.ch:1337/announce",
    "udp://tracker.therarbg.to:6969/announce",
    "udp://tracker.publictracker.xyz:6969/announce",
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.qu.ax:6969/announce",
    "udp://tracker.peerfect.org:6969/announce",
    "udp://tracker.opentrackr.com:6969/announce",
]
ARIA2C_FLAGS = [
    "--seed-time=0",
    "--file-allocation=none",
    "--console-log-level=warn",
    "--summary-interval=5",
    "--bt-stop-timeout=120",
    "--allow-overwrite=true",
    "--continue=true",
    "--dht-entry-point=router.bittorrent.com:6881",
    "--bt-tracker=" + ",".join(FRESH_TRACKERS),
]

MOVIE_EXTENSIONS = {
    "mp4", "mkv", "avi", "mov", "webm", "m4v", "mpg", "mpeg", "wmv", "flv",
}


class Entry:
    def __init__(self, rank: int, title: str, magnet: str, page_url: str,
                 torrent_file: str | None = None) -> None:
        self.rank = rank
        self.title = title
        self.magnet = magnet
        self.torrent_file = torrent_file  # direct .torrent URL, optional
        self.page_url = page_url
        slug = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")
        self.dir = DOWNLOAD_ROOT / f"{rank:03d}-{slug[:40]}"
        self.shot_dir = SCREENSHOT_ROOT / slug


class Status:
    """Shared state for the single compact status line."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.lock = threading.Lock()
        self.downloads: dict[str, str] = {}
        self.extractions: dict[str, str] = {}
        self.done = 0
        self.failed = 0

    @staticmethod
    def _short(title: str, width: int = 14) -> str:
        return title if len(title) <= width else title[: width - 1] + "…"

    def render(self) -> str:
        with self.lock:
            dl_items = list(self.downloads.items())
            ss_items = list(self.extractions.items())
            done, failed, total = self.done, self.failed, self.total
        parts = []
        for kind, items in (("DL", dl_items), ("SS", ss_items)):
            body = " ".join(f"{self._short(t)} {p}" for t, p in items) or "-"
            parts.append(f"{kind}: {body}")
        return " | ".join(parts) + f" | done {done}/{total} fail {failed}"


def load_state() -> set[int]:
    done: set[int] = set()
    if STATE_FILE.exists():
        for line in STATE_FILE.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("status") == "done":
                done.add(record["rank"])
    return done


def load_skipped() -> set[int]:
    """Ranks marked skipped (e.g. dead magnet awaiting a replacement)."""
    skipped: set[int] = set()
    if STATE_FILE.exists():
        for line in STATE_FILE.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("status") == "skipped":
                skipped.add(record["rank"])
    return skipped


def append_state(record: dict[str, object]) -> None:
    with STATE_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def select_entries(
    limit: int, already_done: set[int], skipped_ranks: set[int]
) -> tuple[list[Entry], list[str]]:
    if not TORRENTS_JSON.exists():
        raise RuntimeError(f"{TORRENTS_JSON} not found; run fetch_torrents.py first")
    records = json.loads(TORRENTS_JSON.read_text(encoding="utf-8"))
    records.sort(key=lambda r: r.get("rank", 0))

    entries: list[Entry] = []
    skipped: list[str] = []
    for record in records:
        if len(entries) >= limit:
            break
        rank = record.get("rank")
        title = record.get("title") or f"rank-{rank}"
        if rank in skipped_ranks:
            skipped.append(f"{rank}. {title} (skipped, awaiting replacement torrent)")
            continue
        if rank in already_done:
            skipped.append(f"{rank}. {title} (already screenshotted)")
            continue
        magnet = record.get("magnet") or ""
        if magnet == "Na" or not magnet.startswith("magnet:"):
            skipped.append(f"{rank}. {title} (no magnet in torrents.json)")
            continue
        entries.append(Entry(
            rank, title, magnet, record.get("page_url") or "Na",
            record.get("torrent_file") or None,
        ))
    return entries, skipped


def largest_movie_file(directory: Path) -> Path | None:
    candidates = [
        path
        for path in directory.rglob("*")
        if path.is_file() and path.suffix.lstrip(".").lower() in MOVIE_EXTENSIONS
    ]
    return max(candidates, key=lambda path: path.stat().st_size, default=None)


def download_movie(entry: Entry, status: Status, report) -> Path:
    """Download one torrent with aria2c; return the largest video file."""
    entry.dir.mkdir(parents=True, exist_ok=True)
    # A direct .torrent URL is preferred over the magnet when available:
    # it skips metadata resolution, which stalls on magnets whose swarm
    # only lives in stale 1337x tracker lists.
    source = entry.torrent_file or entry.magnet
    command = [
        "aria2c",
        *ARIA2C_FLAGS,
        "-d", str(entry.dir),
        source,
    ]
    report("meta")
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
    except FileNotFoundError:
        raise RuntimeError("aria2c not found; install it (e.g. pacman -S aria2)")
    assert process.stdout is not None

    # Skip-rule: a download that crawls below MIN_DL_SPEED for longer than
    # STALL_LIMIT is abandoned and logged; the slot moves to the next movie.
    MIN_DL_SPEED = 50 * 1024  # 50 KB/s
    STALL_LIMIT = 240.0  # 4 minutes
    speed_units = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3}
    last_active = time.monotonic()
    for line in process.stdout:
        match = re.search(r"\((\d+)%\)", line)
        if match:
            report(f"{match.group(1)}%")
        speed = re.search(r"DL:([\d.]+)([KMG]?)i?B", line)
        if speed:
            rate = float(speed.group(1)) * speed_units[speed.group(2)]
            if rate >= MIN_DL_SPEED:
                last_active = time.monotonic()
        if time.monotonic() - last_active > STALL_LIMIT:
            process.terminate()
            process.wait()
            raise RuntimeError(
                "download stalled below 50 KB/s for >4 minutes; skipped"
            )
    process.wait()

    control_files = list(entry.dir.rglob("*.aria2"))
    movie = largest_movie_file(entry.dir)
    if process.returncode != 0:
        if control_files or movie is None:
            raise RuntimeError(f"aria2c failed (exit {process.returncode})")
        print(
            f"warning: aria2c exit {process.returncode} for {entry.title}, "
            "but a movie file is present; continuing",
            file=sys.stderr,
        )
    if movie is None:
        raise RuntimeError("no video file found in download")
    report(None)  # hand off to extraction; slot is free
    return movie


def extract_frames(entry: Entry, movie: Path, report) -> int:
    """Extract 2fps frames from movie; returns frame count (resumable)."""
    out_dir = entry.shot_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    total = frame_total(movie)
    saved = min(saved_frames(out_dir), total)
    if saved < total:
        # The last JPEG may be incomplete if a previous run was interrupted.
        if saved > 0:
            (out_dir / frame_name(saved)).unlink(missing_ok=True)
            saved -= 1

        start_seconds = saved / FPS
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-ss", f"{start_seconds:.3}",
            "-i", str(movie),
            "-vf", VIDEO_FILTER,
            "-q:v", "5",
            "-start_number", str(saved + 1),
            "-progress", "pipe:1",
            "-nostats",
            str(out_dir / "frame_%06d.jpg"),
        ]
        try:
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
        except FileNotFoundError:
            raise RuntimeError("ffmpeg not found")
        assert process.stdout is not None
        for line in process.stdout:
            if line.startswith("frame="):
                try:
                    frame = int(line[len("frame="):])
                except ValueError:
                    continue
                percent = (saved + frame) * 100 // total if total else 100
                report(f"{min(percent, 100)}%")
        process.wait()
        if process.returncode != 0:
            stderr = process.stderr.read() if process.stderr else ""
            detail = (
                f": {stderr.strip().splitlines()[-1]}" if stderr.strip() else ""
            )
            raise RuntimeError(
                f"ffmpeg failed (exit {process.returncode}){detail}"
            )

    return saved_frames(out_dir)


class Pipeline:
    def __init__(self, entries: list[Entry]) -> None:
        self.entries = entries
        self.status = Status(len(entries))
        self.download_queue: queue.Queue[Entry] = queue.Queue()
        self.extract_queue: queue.Queue[tuple[Entry, Path]] = queue.Queue()
        self.state_lock = threading.Lock()
        self.remaining = len(entries)
        self.finished = threading.Event()

    def finish_one(self, failed: bool = False) -> None:
        with self.state_lock:
            self.remaining -= 1
            if failed:
                self.status.failed += 1
            else:
                self.status.done += 1
            if self.remaining <= 0:
                self.finished.set()

    def download_worker(self) -> None:
        while True:
            try:
                entry = self.download_queue.get_nowait()
            except queue.Empty:
                return
            report = self.make_download_report(entry)
            try:
                movie = download_movie(entry, self.status, report)
                self.extract_queue.put((entry, movie))
            except Exception as error:
                report(None)
                print(f"\nfail [{entry.title}]: {error}", file=sys.stderr)
                append_state(
                    {"rank": entry.rank, "title": entry.title,
                     "status": "failed", "reason": str(error)[:200]}
                )
                self.finish_one(failed=True)
                shutil.rmtree(entry.dir, ignore_errors=True)

    def make_download_report(self, entry: Entry):
        def report(progress: str | None) -> None:
            with self.status.lock:
                if progress is None:
                    self.status.downloads.pop(entry.title, None)
                else:
                    self.status.downloads[entry.title] = progress
        return report

    def make_extract_report(self, entry: Entry):
        def report(progress: str | None) -> None:
            with self.status.lock:
                if progress is None:
                    self.status.extractions.pop(entry.title, None)
                else:
                    self.status.extractions[entry.title] = progress
        return report

    def extract_worker(self) -> None:
        while True:
            item = self.extract_queue.get()
            if item is None:
                self.extract_queue.task_done()
                return
            entry, movie = item
            report = self.make_extract_report(entry)
            try:
                frames = extract_frames(entry, movie, report)
                with COMPLETED_LOG.open("a", encoding="utf-8") as fh:
                    fh.write(
                        f"{entry.rank}. {entry.title} - {entry.page_url} - {frames} frames\n"
                    )
                append_state(
                    {"rank": entry.rank, "title": entry.title,
                     "status": "done", "frames": frames}
                )
                self.finish_one()
            except Exception as error:
                print(f"\nfail [{entry.title}]: {error}", file=sys.stderr)
                append_state(
                    {"rank": entry.rank, "title": entry.title,
                     "status": "failed", "reason": str(error)[:200]}
                )
                self.finish_one(failed=True)
            finally:
                report(None)
                shutil.rmtree(entry.dir, ignore_errors=True)
                self.extract_queue.task_done()

    def run(self) -> int:
        for entry in self.entries:
            self.download_queue.put(entry)

        def render_loop() -> None:
            while not self.finished.is_set():
                sys.stdout.write("\r" + self.status.render() + "   ")
                sys.stdout.flush()
                time.sleep(0.5)
            sys.stdout.write("\r" + self.status.render() + "   \n")

        renderer = threading.Thread(target=render_loop, daemon=True)
        renderer.start()

        download_threads = [
            threading.Thread(target=self.download_worker, daemon=True)
            for _ in range(DOWNLOAD_JOBS)
        ]
        extract_threads = [
            threading.Thread(target=self.extract_worker, daemon=True)
            for _ in range(EXTRACT_JOBS)
        ]
        for thread in download_threads + extract_threads:
            thread.start()
        for thread in download_threads:
            thread.join()
        for _ in extract_threads:
            self.extract_queue.put(None)
        for thread in extract_threads:
            thread.join()
        renderer.join()

        return 1 if self.status.failed else 0


def main() -> int:
    limit = 6
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(
            "Usage: pipeline.py [limit]\n\n"
            f"Downloads the first [limit] (default {limit}) movies from "
            f"{TORRENTS_JSON} with aria2c and extracts 2fps screenshots,\n"
            f"2 downloads + 2 extractions at a time. Logs finished movies to "
            f"{COMPLETED_LOG} and {STATE_FILE}; resumes on re-run."
        )
        return 0
    if args:
        try:
            limit = int(args[0])
        except ValueError:
            print(f"error: invalid limit {args[0]!r}", file=sys.stderr)
            return 2

    already_done = load_state()
    skipped_ranks = load_skipped()
    entries, skipped = select_entries(limit, already_done, skipped_ranks)
    for note in skipped:
        print(f"skip {note}")
    if not entries:
        print("nothing to do: all selected movies are already done")
        return 0

    DOWNLOAD_ROOT.mkdir(exist_ok=True)
    print(
        f"Processing {len(entries)} movie(s): "
        f"{DOWNLOAD_JOBS} downloads and {EXTRACT_JOBS} extractions at a time."
    )
    return Pipeline(entries).run()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted; partial downloads resume on the next run.", file=sys.stderr)
        sys.exit(130)
