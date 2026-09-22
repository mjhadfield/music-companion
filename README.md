# Music Companion

A personal, visual database linking three sides of a music life:

- **Physical** — vinyl collection (~250 records), via Discogs CSV export
- **Digital** — listening history (~100k scrobbles), via the Last.fm API
- **Live** — gig history (367 setlists), via the Setlist.fm API

Goal: look up a song and see everywhere it shows up — owned on vinyl,
heard live 6 times, full setlists from each show, personal notes — plus
open-ended stats and "rabbit hole" browsing. Eventually: reviews/journal
entries per band/album/song, and Spotify integration (playlists, playback).

## Architecture

**SQLite is the database, full stop.** `data/music.sqlite` (gitignored,
built by the ETL scripts) is the single source of truth, queryable
directly with SQL locally.

The plan for hosting: ship that same `.sqlite` file as a static asset and
query it **in the browser** via `sql.js` (SQLite compiled to WebAssembly).
That means the whole site can live on free static hosting (GitHub Pages) —
no backend, no server costs — while still giving "real SQL" queries like
*"songs heard live ≥5 times that I also own on vinyl"* as simple JOINs
rather than bespoke app logic. Periodic updates happen via a GitHub
Actions cron job that re-runs the ETL scripts, rebuilds the DB, commits it,
and lets Pages redeploy.

See [`schema.sql`](schema.sql) for the full data model. Short version:
canonical `artists` / `albums` / `songs` tables (keyed by MusicBrainz ID
where known — that's what lets the same song matched from Discogs,
Last.fm, and Setlist.fm resolve to *one* row), linked out to
`vinyl_holdings`, `scrobbles`, and `setlists` / `setlist_songs`. Raw pulls
from each source are kept untouched in `staging_*` tables so re-running
matching logic doesn't require re-hitting the APIs. `alias_overrides`
is a manual fixup table for anything automatic name-matching gets wrong.

## Project layout

```
schema.sql              -- the data model (source of truth for structure)
etl/
  init_db.py             -- builds/rebuilds data/music.sqlite from schema.sql
  common.py                -- shared get-or-create matching helpers + .env loading
  discogs_import.py       -- imports a Discogs collection CSV export
  lastfm_pull.py           -- pulls scrobble history from the Last.fm API
  setlistfm_pull.py         -- pulls attended setlists from the Setlist.fm API
  build_public_db.py        -- strips staging_* tables -> site/public/music.sqlite
  (entity_resolution.py -- coming next)
data/
  raw/                   -- raw exports (gitignored except the sample)
  music.sqlite            -- the working database, staging tables and all (gitignored)
imports/                 -- your real personal exports (gitignored entirely)
site/                    -- static frontend: plain HTML/CSS/JS, no build step
  index.html               -- shell + sql.js script tag
  css/style.css             -- theme (light/dark via prefers-color-scheme)
  js/app.js                  -- hash router + every view's SQL query
  public/music.sqlite        -- the slim db the browser actually downloads (gitignored)
.github/workflows/       -- cron-based refresh automation, not started yet
.env                     -- real API keys (gitignored; see .env.example)
```

## Status

- [x] Schema designed
- [x] Discogs CSV import (naive artist/album matching — good enough to get
      data in the door; MusicBrainz-based entity resolution across all
      three sources is a later pass)
- [x] Last.fm scrobble pull (`etl/lastfm_pull.py` — incremental by default,
      `--full` for a from-scratch history pull; 97,500 scrobbles imported)
- [x] Setlist.fm setlist pull (`etl/setlistfm_pull.py` — always does a full
      re-pull since setlists get edited after the fact and the dataset is
      small; 367 setlists / 4,832 song entries imported, covers resolved
      to their original artist)
- [ ] Entity resolution / MBID matching pass
- [x] Static frontend (`site/` — sql.js + plain JS, no build step; home
      dashboard, artist/song/setlist pages, live search; verified working
      end-to-end with real data via a headless-browser smoke test)
- [x] Deployed to GitHub Pages (`.github/workflows/pages.yml`, deploys
      `site/` on every push to `master`). `site/public/music.sqlite` is
      committed on purpose — see the Publishing section below
- [x] Stats & drill-down browsing (the original "click a song, see it
      heard live 6 times, owned on vinyl" flow — works)
- [x] UI polish: dark mode toggle (persisted, validated for CVD-safety
      against this site's actual surfaces via the dataviz skill's
      validator); charts with a Day/Month/Year/All granularity toggle
      (All = full history by year; each step in trades range for finer
      buckets) and click-a-bar-to-filter, with a clear pill; six
      filterable/sortable browse pages linked from every home stat card
      (`#/artists`, `#/vinyl`, `#/shows`, `#/scrobbles`, `#/songs` —
      one row per song, `#/venues` — one row per venue with visit count
      + last-visited date) plus an album detail page (`#/album/:id`,
      every physical copy owned + cover art) that vinyl entries link to
- [x] External enrichment beyond personal stats: artist bio + genre tags
      (MusicBrainz MBID lookup → Wikipedia summary, name-search fallback
      for artists without an MBID yet) and album cover art (Cover Art
      Archive, keyed off whichever MBID an album has). Fetched lazily
      client-side per page view, cached in localStorage 30 days — no
      backend, no bulk pre-fetching against either API
- [ ] Notes/journal writing UI
- [ ] GitHub Actions cron refresh
- [ ] Spotify integration

## Getting started (current state)

```bash
cp .env.example .env             # then fill in your API keys/username

python3 etl/init_db.py --fresh          # build data/music.sqlite from schema.sql
python3 etl/discogs_import.py imports/your-export.csv   # Discogs collection CSV
python3 etl/lastfm_pull.py --full                        # full scrobble history
python3 etl/lastfm_pull.py                                # later: incremental top-up
python3 etl/setlistfm_pull.py                             # attended setlists (always a full re-pull)
```

To import your real Discogs collection: export it from discogs.com →
Collection → Export → CSV, drop it in `imports/` (gitignored) or
`data/raw/` (also gitignored), and point `discogs_import.py` at it.

Last.fm needs a free API key from last.fm/api/account/create — only a
plain key is required, no OAuth/callback flow, since we only read public
scrobble history.

### Ongoing maintenance

Once the database exists, `etl/refresh.py` is the normal way to pull in
new data — it wraps whichever pullers you ask for and prints a
before/after "what's new" report (new scrobbles + top artists among
them, the actual list of new setlists and new vinyl holdings, and any
brand-new artists — flagged if one looks like a near-duplicate of an
existing artist under a different spelling, e.g. "Motorhead" vs
"Motörhead", so entity-resolution mistakes get caught before they're
published rather than after):

```bash
python3 etl/refresh.py --lastfm
python3 etl/refresh.py --setlistfm
python3 etl/refresh.py --discogs imports/new-export.csv
python3 etl/refresh.py --lastfm --setlistfm --discogs imports/new-export.csv   # any combination
```

Read the report. If it looks right:

```bash
python3 etl/build_public_db.py   # refresh site/public/music.sqlite
git add -A && git commit ...      # your call, whenever you're ready
```

### Running the frontend locally

```bash
./run.sh              # builds site/public/music.sqlite, serves on :8642
PORT=3000 ./run.sh     # or pick a different port
```

(Must be served over HTTP, not opened as a `file://` path, since the page
`fetch()`es the database file.)

`site/public/music.sqlite` is a slim export (core tables only, no raw
staging JSON). Unlike `data/music.sqlite` (the working copy, with every
raw API response) it's a tracked file, not gitignored -- publishing it
was a deliberate decision, made once real listening/purchase/gig data
was already sitting in it.

### Querying the database directly

```bash
python3 etl/explore/server.py   # serves on :8644
```

A read-only SQL browser for `data/music.sqlite` (the full working copy,
not the slim public export) -- write your own query against any table
and see the results, with autocomplete for table and column names
(alias-aware: `FROM artists a` then typing `a.` suggests artists' own
columns) and a schema sidebar to click a table or column straight into
the editor. Genuinely read-only in two independent layers: the database
is opened with SQLite's own `mode=ro` flag, and a query is refused up
front unless it starts with `SELECT`/`WITH`. Every query also runs under
a row cap and a wall-clock timeout, so nothing can lock up the tool.

Deliberately styled to match Citadel (the LAN dashboard this links from)
rather than this project's own site -- it's linked from there as its own
project, both start/stop and a Launchpad tile.

## Publishing / GitHub Pages

`.github/workflows/pages.yml` deploys everything under `site/` (the
frontend code *and* the committed `music.sqlite`) to GitHub Pages on
every push to `master`. One manual, one-time step this repo's own
history can't do for you: in the repo's **Settings → Pages**, set
"Build and deployment" → **Source** to **GitHub Actions** (rather than
"Deploy from a branch") -- after that, every push deploys automatically
and the Actions tab shows each run.

To publish an update after re-running any ETL script:

```bash
python3 etl/build_public_db.py   # refresh site/public/music.sqlite
git add site/public/music.sqlite
git commit -m "Refresh public data"
git push
```

Automating that refresh (a scheduled Action that pulls new
scrobbles/setlists, rebuilds the public db, and commits it) is the
still-open "GitHub Actions cron refresh" item above -- for now it's a
manual step.
