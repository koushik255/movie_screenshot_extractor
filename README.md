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
# movie_screenshot_extractor
