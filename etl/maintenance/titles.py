"""
Title normalisation shared by every duplicate scan and suggestion in the maintenance tool.

The same record turns up under many spellings -- "Into the Void - 2009 Remaster", "Paranoid
(Remastered)", "Heaven and Hell [Deluxe Edition]" -- and a plain fuzzy score can't tell those
apart from genuinely different records ("Van Halen" vs "Van Halen II"). So instead of scoring
raw strings, titles are split into a *base* plus the *tags* peeled off their trailing
" - suffix" / "(suffix)" / "[suffix]" parts:

  * edition tags ("remaster", "deluxe", "mono", "single version", ...) describe a different
    issue of the same recording -- safe to fold together;
  * variant tags ("live", "demo", "remix", "edit", "acoustic", ...) describe a genuinely
    different performance/mix -- still the same song, but only merged after explicit review.

A suffix that matches neither list is left on the base: unknown text is treated as meaningful,
never silently discarded.
"""
import re
import unicodedata

_EDITION_PATTERNS = [
    r"\bremaster", r"\bre-?master", r"\bdeluxe\b", r"\bexpanded\b", r"\banniversary\b",
    r"\bedition\b", r"\breissue\b", r"\bmono\b", r"\bstereo\b", r"\bbonus\b",
    r"\b(single|album|lp|original|us|uk)\s+(version|mix)\b", r"\bexplicit\b", r"\bclean\b",
    r"\bfeat\.?\b", r"\bft\.?\b", r"\bfeaturing\b", r"\bdigital(ly)?\b", r"^\d{4}$",
    r"\bsoundtrack\b", r"\bmotion picture\b", r"\bhidden track\b",
]
_VARIANT_PATTERNS = {
    "live": r"\blive\b|\bin concert\b",
    "demo": r"\bdemos?\b|\brehearsal\b|\bouttakes?\b|\bearly\b|\bpre.?production\b",
    "remix": r"\bre-?mix(ed)?\b|\bmix(ed)?\b|\brmx\b|\bdub\b|\bvip\b",
    "edit": r"\bedit\b|\bextended\b",
    "rerecording": r"\bre-?record(ed|ing)?\b|\btaylor.s version\b",
    "acoustic": r"\bacoustic\b|\bunplugged\b",
    "instrumental": r"\binstrumental\b|\bkaraoke\b",
    "session": r"\bsessions?\b|\btake\b|\bspotify singles\b",
    "version": r"\bversion\b",
}
_EDITION_RE = re.compile("|".join(_EDITION_PATTERNS), re.IGNORECASE)
_VARIANT_RES = {tag: re.compile(p, re.IGNORECASE) for tag, p in _VARIANT_PATTERNS.items()}
_BRACKET_TAIL = re.compile(r"\s*[\(\[]([^()\[\]]*)[\)\]]\s*$")

ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}


def fold(text: str) -> str:
    """Casefold + strip accents + punctuation -> single spaces. The comparison form of any text."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    text = text.replace("&", " and ")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_artist_name(name: str) -> str:
    """"The Beatles" / "Beatles, The" / "beatles" all -> "beatles"; "Toots & The Maytals" ->
    "toots and the maytals"."""
    folded = fold(name)
    folded = re.sub(r"^the ", "", folded)
    folded = re.sub(r" the$", "", folded)  # "Beatles, The"
    return folded


def _classify(suffix: str) -> set[str] | None:
    """Tags for one peeled suffix: {"edition"} / {"live", ...} / None if unrecognised."""
    tags = {tag for tag, rx in _VARIANT_RES.items() if rx.search(suffix)}
    # "Single Version"/"Album Version" is an edition, not the generic "version" variant.
    if _EDITION_RE.search(suffix):
        tags.discard("version")
        if not tags:
            return {"edition"}
        return tags | {"edition"}
    return tags or None


def split_title(title: str) -> tuple[str, set[str]]:
    """-> (base title as written, tags). Peels recognised suffixes from the right only."""
    base = (title or "").strip()
    tags: set[str] = set()
    while True:
        m = _BRACKET_TAIL.search(base)
        if m and m.start() > 0:
            found = _classify(m.group(1))
            if found:
                tags |= found
                base = base[: m.start()].rstrip()
                continue
        idx = base.rfind(" - ")
        if idx > 0:
            found = _classify(base[idx + 3:])
            if found:
                tags |= found
                base = base[:idx].rstrip()
                continue
        return base, tags


def base_key(title: str) -> str:
    """The grouping key: folded base title with edition/variant suffixes removed."""
    return fold(split_title(title)[0])


def variant_tags(title: str) -> set[str]:
    return split_title(title)[1] - {"edition"}


def sequel_marker(title: str) -> str | None:
    """"Van Halen II" -> "2", "Vol. 4" -> "4", "Led Zeppelin IV" -> "4"; None when the title
    doesn't end in a number. Two titles with different markers are different records however
    similar the rest is."""
    words = fold(split_title(title)[0]).split()
    if not words:
        return None
    last = words[-1]
    if last.isdigit() and len(last) <= 2:
        return str(int(last))
    if last in ROMAN and len(words) > 1:
        return str(ROMAN[last])
    return None
