#!/usr/bin/env python3
"""Extract ranked movies from a Letterboxd poster-list in saved HTML."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import TextIO


TARGET_UL_CLASSES = {"poster-list", "-p125", "-grid"}


class LetterboxdPosterParser(HTMLParser):
    """Parse movie metadata from Letterboxd's poster grid markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.target_ul_depth = 0
        self.current: dict[str, object] | None = None
        self.in_list_number = False
        self.list_number_parts: list[str] = []
        self.movies: list[dict[str, object]] = []

    @staticmethod
    def _attributes(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {key: value or "" for key, value in attrs}

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = self._attributes(attrs)
        classes = set(attributes.get("class", "").split())

        if tag == "ul" and TARGET_UL_CLASSES.issubset(classes):
            self.target_ul_depth += 1
            return

        if not self.target_ul_depth:
            return

        if tag == "ul":
            self.target_ul_depth += 1

        if tag == "li" and {"posteritem", "numbered-list-item"}.issubset(classes):
            self.current = {}
            self.in_list_number = False
            self.list_number_parts = []

        if self.current is None:
            return

        full_name = attributes.get("data-item-full-display-name")
        item_name = attributes.get("data-item-name")
        if full_name or item_name:
            self.current["name"] = full_name or item_name or ""
            self.current["slug"] = attributes.get("data-item-slug", "")
            self.current["link"] = attributes.get(
                "data-item-link", attributes.get("data-target-link", "")
            )
            list_index = attributes.get("data-list-index", "")
            if list_index.isdigit():
                self.current["index"] = int(list_index) + 1

        if tag == "p" and "list-number" in classes:
            self.in_list_number = True
            self.list_number_parts = []

    def handle_data(self, data: str) -> None:
        if self.current is not None and self.in_list_number:
            self.list_number_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self.target_ul_depth:
            return

        if tag == "p" and self.current is not None and self.in_list_number:
            number = "".join(self.list_number_parts).strip()
            if number.isdigit():
                self.current["index"] = int(number)
            self.in_list_number = False

        if tag == "li" and self.current is not None:
            if self.current.get("name"):
                self.movies.append(self.current)
            self.current = None
            self.in_list_number = False

        if tag == "ul":
            self.target_ul_depth -= 1


class SourceViewParser(HTMLParser):
    """Reconstruct source saved from Chromium's ``view-source:`` display."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_source_line = False
        self.source_line_depth = 0
        self.parts: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        classes = set(dict(attrs).get("class", "").split())
        if tag == "td" and "line-content" in classes:
            self.in_source_line = True
            self.source_line_depth = 1
        elif self.in_source_line and tag != "br":
            self.source_line_depth += 1

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        # Chromium uses a real <br> element to represent an empty source line.
        if self.in_source_line and tag != "br":
            return

    def handle_data(self, data: str) -> None:
        if self.in_source_line:
            self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if self.in_source_line:
            self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self.in_source_line:
            self.parts.append(f"&#{name};")

    def handle_endtag(self, tag: str) -> None:
        if not self.in_source_line:
            return
        self.source_line_depth -= 1
        if tag == "td" and self.source_line_depth == 0:
            self.parts.append("\n")
            self.in_source_line = False


def unwrap_saved_source(source: str) -> str:
    """Return original HTML when a browser's rendered source page was saved."""
    if 'class="line-content"' not in source:
        return source
    parser = SourceViewParser()
    parser.feed(source)
    parser.close()
    return "".join(parser.parts)


def parse_movies(source: str) -> list[dict[str, object]]:
    source = unwrap_saved_source(source)
    parser = LetterboxdPosterParser()
    parser.feed(source)
    parser.close()
    return parser.movies


def write_movies(
    movies: list[dict[str, object]], output_format: str, output: TextIO
) -> None:
    if output_format == "json":
        json.dump(movies, output, ensure_ascii=False, indent=2)
        output.write("\n")
        return

    if output_format == "csv":
        writer = csv.DictWriter(output, fieldnames=["index", "name", "slug", "link"])
        writer.writeheader()
        writer.writerows(movies)
        return

    for movie in movies:
        output.write(f"{movie.get('index', '?')}. {movie['name']}\n")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract movies from Letterboxd poster-list HTML."
    )
    parser.add_argument(
        "html_files",
        nargs="*",
        help="saved HTML file(s); omit them or use '-' to read HTML from stdin",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=("text", "csv", "json"),
        default="text",
        help="output format (default: text)",
    )
    parser.add_argument(
        "-o", "--output", help="write to this file instead of standard output"
    )
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()

    try:
        if not args.html_files or args.html_files == ["-"]:
            sources = [sys.stdin.read()]
        elif "-" in args.html_files:
            print("error: '-' cannot be combined with file paths", file=sys.stderr)
            return 2
        else:
            sources = [
                Path(html_file).read_text(encoding="utf-8")
                for html_file in args.html_files
            ]
    except (OSError, UnicodeError) as error:
        print(f"error: could not read HTML: {error}", file=sys.stderr)
        return 2

    movies = [movie for source in sources for movie in parse_movies(source)]
    if not movies:
        print(
            "error: no movies found in a <ul> with classes "
            "'poster-list -p125 -grid'",
            file=sys.stderr,
        )
        return 1

    movies.sort(key=lambda movie: int(movie.get("index", 0)))
    ranks = [movie.get("index") for movie in movies if "index" in movie]
    duplicate_ranks = sorted({rank for rank in ranks if ranks.count(rank) > 1})
    if duplicate_ranks:
        print(
            "error: duplicate movie ranks found: "
            + ", ".join(str(rank) for rank in duplicate_ranks),
            file=sys.stderr,
        )
        return 1

    try:
        if args.output:
            with Path(args.output).open("w", encoding="utf-8", newline="") as output:
                write_movies(movies, args.format, output)
        else:
            write_movies(movies, args.format, sys.stdout)
    except OSError as error:
        print(f"error: could not write output: {error}", file=sys.stderr)
        return 2


    print(
        f"Extracted {len(movies)} movies from {len(sources)} source file(s).",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
