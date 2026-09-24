"""
Local cover-art cache: data/covers/{album_id}.jpg, one real fetch per album, ever --
replacing the old approach of the frontend hitting the Cover Art Archive live, from every
visitor's own browser, on every page view.

Two ways an image gets here, both funnelling through _save()/_mark() so there's one place
that decides the on-disk name and touches the database:
  - fetch_from_cover_art_archive(): automatic, by the album's own mbid (tries /release/ then
    /release-group/, mirroring the two-step attempt the frontend used to make live -- we don't
    always know which kind of mbid an album has).
  - fetch_from_url(): manual, a pasted image URL -- for the ones CAA genuinely doesn't have
    (a scanned sleeve, a pressing CAA never got a photo of, a non-owned album entirely, once
    that's wired up).

Always saved as {id}.jpg regardless of the source's real format (CAA is JPEG in practice
anyway) -- browsers decode <img> content by sniffing actual bytes, not trusting the
extension/declared Content-Type, so this is a real simplification, not a lossy conversion:
nothing here re-encodes the image, it's just a filename choice. Keeps the frontend's own
lookup a single hardcoded pattern (`public/covers/{id}.jpg`) with no extension to track.
"""
import os
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
# Same per-process override idea as common.MUSIC_DB_PATH -- a test instance gets its own covers dir.
COVERS_DIR = Path(os.environ["MUSIC_COVERS_DIR"]) if os.environ.get("MUSIC_COVERS_DIR") else ROOT / "data" / "covers"
MAX_COVER_BYTES = 5 * 1024 * 1024  # generous for one cover thumbnail; guards against something absurd
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
_HEADERS = {"User-Agent": "MusicCompanion/1.0 (personal-use maintenance tool)"}


def _save(album_id: int, content: bytes) -> Path:
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    path = COVERS_DIR / f"{album_id}.jpg"
    path.write_bytes(content)
    return path


def _mark(conn, album_id: int, status: str) -> None:
    conn.execute(
        "UPDATE albums SET cover_status = ?, cover_updated_at = datetime('now') WHERE id = ?",
        (status, album_id),
    )


def has_local_cover(album_id: int) -> bool:
    return (COVERS_DIR / f"{album_id}.jpg").is_file()


def _looks_like_image(resp: requests.Response) -> bool:
    content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
    return content_type in ALLOWED_CONTENT_TYPES and len(resp.content) <= MAX_COVER_BYTES


def fetch_from_cover_art_archive(conn, album_id: int, mbid: str) -> bool:
    """Returns True if real art was found and saved; marks cover_status either way, so a
    completeness sweep never re-tries an album that's genuinely got nothing on CAA."""
    for kind in ("release", "release-group"):
        try:
            resp = requests.get(
                f"https://coverartarchive.org/{kind}/{mbid}/front-500",
                timeout=10, allow_redirects=True, headers=_HEADERS,
            )
        except requests.RequestException:
            continue
        if resp.status_code == 200 and _looks_like_image(resp):
            _save(album_id, resp.content)
            _mark(conn, album_id, "ok")
            return True
    _mark(conn, album_id, "none")
    return False


def fetch_from_url(conn, album_id: int, url: str) -> tuple[bool, str | None]:
    """Manual override. Returns (ok, error_message) -- error_message is set only when ok is
    False, for the maintenance UI to show back to whoever pasted the url."""
    try:
        resp = requests.get(url, timeout=10, headers=_HEADERS)
    except requests.RequestException as exc:
        return False, str(exc)
    if resp.status_code != 200:
        return False, f"that url returned HTTP {resp.status_code}"
    if not _looks_like_image(resp):
        content_type = resp.headers.get("Content-Type", "(none)")
        return False, f"doesn't look like an image (content-type: {content_type})"
    _save(album_id, resp.content)
    _mark(conn, album_id, "ok")
    return True, None
