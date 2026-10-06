"""Stamp a new version on the Music Companion site, so every browser picks up the change.

The site is served by a plain `python -m http.server`, which sends no cache headers: browsers keep
their own copies of the page, scripts and styles for a while. This stamps a fresh version into

  * site/index.html -- SITE_VERSION, and ?v=<version> on every local stylesheet and script, so a
    browser with the new page can never use an old cached script;
  * site/version.txt -- read on every load with caching off; a page whose SITE_VERSION differs
    (an old copy from the browser's cache) fetches everything fresh and reloads, once.

Between bumps everything caches as normal. Run it after changing anything in site/ (css, js, html):

    python3 etl/bump_site_version.py
"""
from __future__ import annotations

import re
import time
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent / "site"


def bump() -> str:
    version = time.strftime("%Y%m%d-%H%M%S")
    index = SITE / "index.html"
    html = index.read_text(encoding="utf-8")
    html, n = re.subn(r'const SITE_VERSION = "[^"]*";', f'const SITE_VERSION = "{version}";', html)
    if n != 1:
        raise SystemExit("index.html: SITE_VERSION not found")
    # local css / js only (not the CDN's sql.js), with or without an earlier ?v=
    html = re.sub(r'((?:href|src)="(?:css|js)/[^"?]+\.(?:css|js))(?:\?v=[^"]*)?"', rf'\1?v={version}"', html)
    index.write_text(html, encoding="utf-8")
    (SITE / "version.txt").write_text(version + "\n", encoding="utf-8")
    return version


if __name__ == "__main__":
    print(f"site version {bump()}")
