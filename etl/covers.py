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

A third way, automatic but reviewed: the Last.fm import's cover step (etl/cover_step.py) fetches a
cover for each album you've just played into .pending/, and the Inbox's Covers tab accepts it
into place (accept_pending) or turns it down (reject_pending). Size is chosen for space: an album
you've played only a few times gets the archive's 250px thumbnail (~30 KB against ~80 KB at 500px),
and the step swaps in the 500px one once it has FULL_SIZE_PLAYS plays or is on vinyl.

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
PENDING_DIR = COVERS_DIR / ".pending"   # fetched by an import, awaiting review -- never published (sync_covers copies files, not dirs)
REMOVED_DIR = COVERS_DIR / ".removed"   # set aside, not deleted: undo puts it back
SMALL, FULL = 250, 500                  # Cover Art Archive thumbnail sizes, px
FULL_SIZE_PLAYS = 5                     # from this many plays (or a copy on vinyl) an album gets the 500px cover


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


def _set_aside(src: Path, album_id: int) -> str:
    import time
    REMOVED_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{album_id}-{time.time_ns()}.jpg"
    src.rename(REMOVED_DIR / name)
    return name


def remove_cover(conn, album_id: int, status: str | None = None) -> bool:
    """Undo a cover just saved: the file moves aside to .removed/ (not deleted -- nothing the site
    serves, and recoverable) and the album goes back to `status` (None = never looked up).
    -> whether there was a file to move."""
    src = COVERS_DIR / f"{album_id}.jpg"
    moved = src.is_file()
    if moved:
        _set_aside(src, album_id)
    conn.execute("UPDATE albums SET cover_status = ?, cover_updated_at = datetime('now') WHERE id = ?", (status, album_id))
    return moved


def has_local_cover(album_id: int) -> bool:
    return (COVERS_DIR / f"{album_id}.jpg").is_file()


def _looks_like_image(resp: requests.Response) -> bool:
    content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
    return content_type in ALLOWED_CONTENT_TYPES and len(resp.content) <= MAX_COVER_BYTES


def caa_front(mbid: str, size: int = FULL) -> bytes | None:
    """The archive's front cover for an mbid at `size` px, or None. Tries /release/ then
    /release-group/ -- we don't always know which kind of mbid an album has. Network trouble is
    None too: callers treat it as "not this time", never as a crash."""
    for kind in ("release", "release-group"):
        try:
            resp = requests.get(f"https://coverartarchive.org/{kind}/{mbid}/front-{size}",
                                timeout=10, allow_redirects=True, headers=_HEADERS)
        except requests.RequestException:
            continue
        if resp.status_code == 200 and _looks_like_image(resp):
            return resp.content
    return None


def fetch_from_cover_art_archive(conn, album_id: int, mbid: str) -> bool:
    """Returns True if real art was found and saved; marks cover_status either way, so a
    completeness sweep never re-tries an album that's genuinely got nothing on CAA."""
    content = caa_front(mbid, FULL)
    if content:
        _save(album_id, content)
        _mark(conn, album_id, "ok")
        return True
    _mark(conn, album_id, "none")
    return False


def jpeg_width(path: Path) -> int | None:
    """A JPEG's pixel width from its frame header (no imaging library) -- None for anything else
    (a pasted PNG, an upload), which the size upgrade then leaves alone."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:   # no length field
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):   # a start-of-frame
            return int.from_bytes(data[i + 7:i + 9], "big")
        i += 2 + length
    return None


def wanted_size(plays: int, on_vinyl: bool) -> int:
    return FULL if on_vinyl or plays >= FULL_SIZE_PLAYS else SMALL


# -- Reviewed covers: fetched into .pending/ by an import, accepted or turned down in the Inbox --

def pending_path(album_id: int) -> Path:
    return PENDING_DIR / f"{int(album_id)}.jpg"


def save_pending(album_id: int, content: bytes) -> Path:
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    path = pending_path(album_id)
    path.write_bytes(content)
    return path


def accept_pending(conn, album_id: int) -> bool:
    """The pending cover becomes the album's. -> False if there was none to accept."""
    src = pending_path(album_id)
    if not src.is_file():
        return False
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    src.replace(COVERS_DIR / f"{int(album_id)}.jpg")
    _mark(conn, album_id, "ok")
    return True


def reject_pending(album_id: int) -> str | None:
    """Turn a pending cover down: set aside in .removed/. -> its name there (for undo), or None."""
    src = pending_path(album_id)
    return _set_aside(src, album_id) if src.is_file() else None


def drop_pending(album_id: int) -> None:
    """An import's cover that's no longer wanted (the album was merged away, or got a cover another way)."""
    pending_path(album_id).unlink(missing_ok=True)


def return_to_pending(conn, album_id: int, removed_name: str | None = None) -> None:
    """Undo an Inbox cover decision: an accepted cover (removed_name None) or a turned-down one
    (its name in .removed/) goes back to .pending/, and the album back to "not looked up"."""
    if removed_name is not None:
        if "/" in removed_name or not removed_name.startswith(f"{int(album_id)}-"):
            raise ValueError("not a set-aside cover of this album")
        src = REMOVED_DIR / removed_name
    else:
        src = COVERS_DIR / f"{int(album_id)}.jpg"
    if src.is_file():
        PENDING_DIR.mkdir(parents=True, exist_ok=True)
        src.replace(pending_path(album_id))
    conn.execute("UPDATE albums SET cover_status = NULL, cover_updated_at = datetime('now') WHERE id = ?", (album_id,))


def storage() -> dict:
    """What the covers take on disk: published-side files, and those awaiting review."""
    def tally(d: Path, pattern: str) -> tuple[int, int]:
        files = [p for p in d.glob(pattern) if p.is_file()] if d.is_dir() else []
        return len(files), sum(p.stat().st_size for p in files)
    n, b = tally(COVERS_DIR, "*.jpg")
    pn, pb = tally(PENDING_DIR, "*.jpg")
    return {"files": n, "bytes": b, "pendingFiles": pn, "pendingBytes": pb}


def download(url: str) -> tuple[bytes | None, str | None]:
    """-> (image bytes, None) or (None, why not)."""
    try:
        resp = requests.get(url, timeout=15, headers=_HEADERS, allow_redirects=True)
    except requests.RequestException as exc:
        return None, str(exc)
    if resp.status_code != 200:
        return None, f"that url returned HTTP {resp.status_code}"
    if not _looks_like_image(resp):
        return None, f"doesn't look like an image (content-type: {resp.headers.get('Content-Type', '(none)')})"
    return resp.content, None


def sniff_image(content: bytes) -> bool:
    """JPEG / PNG / WebP by their magic bytes (an uploaded file's declared type means nothing)."""
    return content[:3] == b"\xff\xd8\xff" or content[:8] == b"\x89PNG\r\n\x1a\n" or (content[:4] == b"RIFF" and content[8:12] == b"WEBP")


def save_holding_cover(holding_id: int, content: bytes) -> str:
    """One copy's own cover -> its file name. Named by content, so every image gets its own file:
    undoing a change just points back at the previous file (still there), and the site's
    browsers can never show a stale cached one."""
    import hashlib
    name = f"h{int(holding_id)}-{hashlib.sha1(content).hexdigest()[:10]}.jpg"
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    (COVERS_DIR / name).write_bytes(content)
    return name


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
