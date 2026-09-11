#!/usr/bin/env python3
"""Extract frames from movies as JPEG screenshots at 2 fps (one every 0.5s).

Python port of the curtain tool (~/curtain/src/main.rs). Scans a directory
for movies, then drives ffmpeg to save one scaled frame every 0.5 seconds.
Runs are resumable: already-saved frames are counted and the possibly
truncated last frame is re-extracted on the next run.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

MOVIE_EXTENSIONS = (
    "mp4", "mkv", "avi", "mov", "webm", "m4v", "mpg", "mpeg", "wmv", "flv",
)

FPS = 2.0
VIDEO_FILTER = "fps=2,scale='min(1280,iw)':-2"
DEFAULT_PARALLEL_JOBS = 2


def print_usage(program: str) -> None:
    print(
        f"Usage: {program} [--check] [movie-or-directory] [output-directory]\n"
        "\n"
        "With no path, scans ~/Downloads/Movies. A directory scan includes movies directly\n"
        "inside it and movies inside its immediate subdirectories. --check lists the movies\n"
        "and asks for confirmation before starting. Screenshots are saved every 0.5 seconds.",
        file=sys.stderr,
    )


def home_dir() -> Path:
    home = os.environ.get("HOME")
    if not home:
        raise RuntimeError("HOME is not set; provide paths explicitly")
    return Path(home)


def default_movies_dir() -> Path:
    return home_dir() / "Downloads" / "Movies"


def default_output_dir() -> Path:
    return home_dir() / "lbfive" / "screenshots"


def confirm(movies: list[Path]) -> bool:
    print(f"Found {len(movies)} movie(s):")
    for movie in movies:
        print(f"  {movie}")

    try:
        answer = input("\nCreate screenshots for these movies? [y/N] ")
    except EOFError:
        answer = ""
    return answer.strip().lower() in ("y", "yes")


def is_movie(path: Path) -> bool:
    return path.suffix.lstrip(".").lower() in MOVIE_EXTENSIONS


def find_movies(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise RuntimeError(f"could not read {directory}")

    movies: list[Path] = []
    for path in sorted(directory.iterdir()):
        if path.is_file() and is_movie(path):
            movies.append(path)
        elif path.is_dir():
            for child in sorted(path.iterdir()):
                if child.is_file() and is_movie(child):
                    movies.append(child)

    movies.sort()
    return movies


def frame_total(input_path: Path) -> int:
    try:
        output = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1",
                str(input_path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        duration = float(output.stdout.strip())
    except FileNotFoundError:
        raise RuntimeError("could not start ffprobe. Is ffmpeg installed?")
    except (subprocess.CalledProcessError, ValueError):
        raise RuntimeError(f"could not determine duration of {input_path}")
    return int(duration * FPS + 0.999999)  # ceil


def saved_frames(output_dir: Path) -> int:
    frame = 1
    while (output_dir / f"frame_{frame:06}.jpg").is_file():
        frame += 1
    return frame - 1


def frame_name(frame: int) -> str:
    return f"frame_{frame:06}.jpg"


_print_lock = threading.Lock()


class Progress:
    """Per-movie progress that throttles output to avoid flooding the terminal."""

    def __init__(self, name: str, total: int) -> None:
        self.name = name
        self.total = total
        self.position = 0
        self.done = False
        self.message = ""
        self._last_print = 0.0

    def set_position(self, position: int, force: bool = False) -> None:
        self.position = min(position, self.total)
        now = time.monotonic()
        if force or now - self._last_print >= 2.0 or self.position >= self.total:
            self._last_print = now
            self._print()

    def finish(self, message: str) -> None:
        self.done = True
        self.message = message
        self._print(final=True)

    def fail(self, message: str) -> None:
        self.done = True
        self.message = message
        self._print(final=True)

    def _print(self, final: bool = False) -> None:
        percent = (100 * self.position / self.total) if self.total else 100.0
        status = f" ({self.message})" if self.message else ""
        line = (
            f"{self.name:<40} {self.position}/{self.total} [{percent:5.1f}%]{status}"
        )
        with _print_lock:
            print(f"\r{line}" + " " * 10, flush=final)


def extract_frames(input_path: Path, output_dir: Path, progress: Progress) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    total = frame_total(input_path)
    progress.total = total
    saved = min(saved_frames(output_dir), total)
    progress.set_position(saved, force=True)
    if saved >= total:
        progress.finish("already complete")
        return
    # The last JPEG may be incomplete if the previous process was interrupted.
    if saved > 0:
        (output_dir / frame_name(saved)).unlink(missing_ok=True)
        saved -= 1
        progress.set_position(saved, force=True)

    start_seconds = saved / FPS
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-ss", f"{start_seconds:.3}",
        "-i", str(input_path),
        "-vf", VIDEO_FILTER,
        "-q:v", "5",
        "-start_number", str(saved + 1),
        "-progress", "pipe:1",
        "-nostats",
        str(output_dir / "frame_%06d.jpg"),
    ]
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
    except FileNotFoundError:
        raise RuntimeError("could not start ffmpeg. Is ffmpeg installed?")
    assert process.stdout is not None

    for line in process.stdout:
        if line.startswith("frame="):
            try:
                frame = int(line[len("frame="):])
            except ValueError:
                continue
            progress.set_position(saved + frame)

    process.wait()
    if process.returncode == 0:
        progress.set_position(total, force=True)
        progress.finish("complete")
    else:
        stderr = process.stderr.read() if process.stderr else ""
        progress.fail("failed; run again to resume")
        detail = f": {stderr.strip().splitlines()[-1]}" if stderr.strip() else ""
        raise RuntimeError(f"ffmpeg failed with status {process.returncode}{detail}")


def process_movie(
    movie: Path, output_root: Path | None, directory_mode: bool
) -> tuple[Path, Exception | None]:
    stem = movie.stem or "movie"
    output = (output_root if output_root is not None else default_output_dir()) / stem
    if not directory_mode and output_root is not None:
        output = output_root  # single movie given with an explicit output path
    progress = Progress(stem, 0)
    try:
        extract_frames(movie, output, progress)
        return movie, None
    except (RuntimeError, OSError) as error:
        return movie, error


def run() -> int:
    program = "screenshots.py"
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print_usage(program)
        return 0
    check = bool(args) and args[0] == "--check"
    if check:
        args = args[1:]
    if len(args) > 2:
        print_usage(program)
        print("error: too many arguments", file=sys.stderr)
        return 1

    input_path = Path(args[0]) if args else default_movies_dir()
    requested_output = Path(args[1]) if len(args) > 1 else None

    if input_path.is_file():
        movies = [input_path]
    elif input_path.is_dir():
        movies = find_movies(input_path)
        if not movies:
            print(f"error: no supported movies found in {input_path}", file=sys.stderr)
            return 1
    else:
        print(f"error: input does not exist: {input_path}", file=sys.stderr)
        return 1

    if check and not confirm(movies):
        print("Cancelled.")
        return 0

    output_root = requested_output

    raw_jobs = os.environ.get("CURTAIN_JOBS", "")
    jobs = int(raw_jobs) if raw_jobs.isdigit() and int(raw_jobs) > 0 else DEFAULT_PARALLEL_JOBS

    directory_mode = input_path.is_dir()
    print(f"Processing with up to {jobs} movie(s) at once.")
    failures = 0
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [
            pool.submit(process_movie, movie, output_root, directory_mode)
            for movie in movies
        ]
        for future in as_completed(futures):
            movie, error = future.result()
            if error is not None:
                failures += 1
                print(f"error processing {movie}: {error}", file=sys.stderr)

    print(f"Processed {len(movies)} movie(s); {failures} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except KeyboardInterrupt:
        print("\nInterrupted; run again to resume.", file=sys.stderr)
        sys.exit(130)
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
