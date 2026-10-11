/*
 * Enrichment beyond our own personal-stats data: an artist bio + genre
 * tags pulled live from MusicBrainz + Wikipedia. Client-side, no backend,
 * no API key -- both services support CORS for exactly this kind of use.
 *
 * Fetched lazily, one artist at a time, only when its page is actually
 * viewed -- never pre-fetched in bulk, so there's no rate-limit concern
 * against MusicBrainz's "be gentle" unauthenticated-API etiquette.
 * Results are cached in localStorage for 30 days.
 *
 * Artists whose About maintenance has stored (artist_about, about.py) never come here --
 * this is only the fallback for ones not fetched yet. MusicBrainz (keyed by the MBID) gives the
 * artist's Wikidata link, whose English sitelink is the Wikipedia page; no MBID or no page = no
 * About (the old "<name> band" name search guessed wrong too often).
 *
 * Album cover art is NOT looked up here (or live, at all, anymore) -- it's
 * fetched once server-side by the maintenance tool (etl/covers.py) and
 * stored locally under site/public/covers/{albumId}.jpg, shipped as part
 * of the public build like everything else. See attachCoverArt() below.
 */

const ENRICH_TTL_MS = 30 * 24 * 60 * 60 * 1000; // 30 days

function enrichCacheGet(key) {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (Date.now() - parsed.ts > ENRICH_TTL_MS) return null;
    return parsed.data;
  } catch {
    return null;
  }
}

function enrichCacheSet(key, data) {
  try {
    localStorage.setItem(key, JSON.stringify({ ts: Date.now(), data }));
  } catch {
    /* storage full or unavailable -- fine, just skip caching */
  }
}

async function fetchJson(url, { timeoutMs } = {}) {
  const controller = timeoutMs ? new AbortController() : null;
  const timer = timeoutMs ? setTimeout(() => controller.abort(), timeoutMs) : null;
  try {
    const resp = await fetch(url, controller ? { signal: controller.signal } : undefined);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return await resp.json();
  } finally {
    if (timer) clearTimeout(timer);
  }
}

// MusicBrainz is the more accurate lookup (unambiguous MBID, plus genre
// tags) but it's also the one that can be slow or rate-limited (its own
// etiquette asks unauthenticated clients to stay under ~1 req/sec). Capping
// it keeps a single slow response from stalling the whole page instead of
// falling back to the plain Wikipedia name search within a bounded time.
const MUSICBRAINZ_TIMEOUT_MS = 6000;  // now the only route to the page (no name-search fallback)

async function findWikipediaTitleViaMusicBrainz(mbid) {
  const data = await fetchJson(
    `https://musicbrainz.org/ws/2/artist/${mbid}?fmt=json&inc=url-rels+tags`,
    { timeoutMs: MUSICBRAINZ_TIMEOUT_MS }
  );
  const rels = data.relations || [];
  const wiki = rels.find((r) => r.url && /en\.wikipedia\.org\/wiki\//.test(r.url.resource));
  const tags = (data.tags || [])
    .sort((a, b) => b.count - a.count)
    .slice(0, 5)
    .map((t) => t.name);
  let title = wiki ? decodeURIComponent(wiki.url.resource.split("/wiki/")[1]).replace(/_/g, " ") : null;
  // MusicBrainz now links artists to Wikidata rather than Wikipedia: its English sitelink is the page
  const wd = !title && rels.find((r) => r.url && /wikidata\.org\/wiki\/Q\d+/.test(r.url.resource));
  if (wd) {
    const qid = wd.url.resource.match(/Q\d+/)[0];
    const d = await fetchJson(`https://www.wikidata.org/w/api.php?action=wbgetentities&ids=${qid}&props=sitelinks&sitefilter=enwiki&format=json&origin=*`);
    title = d?.entities?.[qid]?.sitelinks?.enwiki?.title || null;
  }
  return { title, tags };
}

async function fetchWikipediaSummary(title) {
  const data = await fetchJson(`https://en.wikipedia.org/api/rest_v1/page/summary/${encodeURIComponent(title)}`);
  if (data.type === "disambiguation") return null;
  return {
    extract: data.extract || null,
    thumbnail: data.thumbnail ? data.thumbnail.source : null,
    pageUrl: data.content_urls ? data.content_urls.desktop.page : null,
  };
}

const EMPTY_ENRICHMENT = { extract: null, thumbnail: null, pageUrl: null, tags: [] };

async function getArtistEnrichment(artist) {
  const cacheKey = `enrich2:artist:${artist.id}`;  // 2: via Wikidata, no name search (old entries could be the wrong band)
  const cached = enrichCacheGet(cacheKey);
  if (cached) return cached;

  try {
    // Run both title-resolution paths at once rather than waiting on
    // MusicBrainz before ever trying the plain (fast, no tags) Wikipedia
    // name search -- so a slow MusicBrainz response no longer means a
    // slow page even when it eventually times out and we fall back.
    // Only the page MusicBrainz points at: the old "<name> band" search took the top hit unchecked
    // and showed the wrong band often enough (maintenance offers it as candidates to confirm instead).
    const mbResult = artist.mbid ? await findWikipediaTitleViaMusicBrainz(artist.mbid).catch(() => null) : null;
    const title = mbResult?.title || null;
    const tags = mbResult?.tags || [];

    const summary = title ? await fetchWikipediaSummary(title) : null;
    const result = summary ? { ...summary, tags } : { ...EMPTY_ENRICHMENT, tags };
    enrichCacheSet(cacheKey, result);
    return result;
  } catch (err) {
    console.warn("Enrichment failed for", artist.name, err);
    // Cache the miss too, so a page revisit doesn't refetch every time.
    enrichCacheSet(cacheKey, EMPTY_ENRICHMENT);
    return EMPTY_ENRICHMENT;
  }
}

/** Point an <img> at an album's locally-cached cover (public/covers/{albumId}.jpg), or
 * remove it from the DOM if this album doesn't have one yet -- the maintenance tool's Vinyl
 * Holdings page is where that gets fixed, not this page fetching it live. */
function attachCoverArt(imgEl, albumId, coverStatus, coverVersion) {
  if (coverStatus !== "ok") {
    imgEl.remove();
    return;
  }
  imgEl.src = coverUrl(albumId, coverVersion);
}

/** A cover's address, stamped with when it last changed (albums.cover_updated_at): replacing a
 * cover in maintenance keeps the same file name, so without the stamp browsers -- and Citadel's
 * iframe, which a hard refresh of the page doesn't reach -- keep showing the old image. */
function coverUrl(albumId, coverVersion) {
  return `public/covers/${albumId}.jpg${coverVersion ? `?v=${encodeURIComponent(coverVersion)}` : ""}`;
}
