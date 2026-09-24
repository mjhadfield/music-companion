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
 * MusicBrainz (keyed by the MBID we already resolved during the Last.fm
 * import -- unambiguous, and it's the source of genre tags) and a plain
 * Wikipedia name search run in parallel rather than one strictly after
 * the other, preferring MusicBrainz's title when it comes back in time;
 * MusicBrainz gets a capped timeout since it's the slower/rate-limited
 * of the two, so a bad response from it never stalls the whole lookup.
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
const MUSICBRAINZ_TIMEOUT_MS = 3000;

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
  const title = wiki ? decodeURIComponent(wiki.url.resource.split("/wiki/")[1]).replace(/_/g, " ") : null;
  return { title, tags };
}

async function findWikipediaTitleByName(name) {
  const data = await fetchJson(
    `https://en.wikipedia.org/w/api.php?action=opensearch&search=${encodeURIComponent(name + " band")}&limit=1&namespace=0&format=json&origin=*`
  );
  return data[1] && data[1][0] ? data[1][0] : null;
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
  const cacheKey = `enrich:artist:${artist.id}`;
  const cached = enrichCacheGet(cacheKey);
  if (cached) return cached;

  try {
    // Run both title-resolution paths at once rather than waiting on
    // MusicBrainz before ever trying the plain (fast, no tags) Wikipedia
    // name search -- so a slow MusicBrainz response no longer means a
    // slow page even when it eventually times out and we fall back.
    const mbPromise = artist.mbid
      ? findWikipediaTitleViaMusicBrainz(artist.mbid).catch(() => null)
      : Promise.resolve(null);
    const namePromise = findWikipediaTitleByName(artist.name).catch(() => null);
    const [mbResult, nameTitle] = await Promise.all([mbPromise, namePromise]);

    const title = mbResult?.title || nameTitle;
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
function attachCoverArt(imgEl, albumId, coverStatus) {
  if (coverStatus !== "ok") {
    imgEl.remove();
    return;
  }
  imgEl.src = `public/covers/${albumId}.jpg`;
}
