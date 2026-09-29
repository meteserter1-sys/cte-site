#!/usr/bin/env python3
"""
CTE News Collector + Classifier
================================

Pipeline (same shape as TradersEQ's loader -> enricher -> page):

  1. COLLECT   Google News RSS searches (multilingual) + hand-picked publisher RSS feeds.
  2. DEDUPE    Stable doc id = sha1(normalised headline + source). Already-seen ids are skipped
               so the LLM is only paid for once per story.
  3. CLASSIFY  Claude reads headline (+ snippet) and answers with strict JSON:
               product / direction / impact / confidence / country / eventType / summary.
               Direction is a PRICE argument for an importer of SBM, corn and wheat into
               South-East Asia — NOT sentiment. "Record Brazil crop" = good news = BEARISH.
  4. CLUSTER   Differently-worded reports of the same event are folded into one document;
               `trend` = number of publishers carrying it. One event, one impact.
  5. WRITE     Firestore collection `news`, one document per EVENT. The CTE page listens
               to that collection with onSnapshot and does the trailing-24h arithmetic itself.

Environment variables
  FIREBASE_SERVICE_ACCOUNT  JSON text of a Firebase service-account key   (required unless --dry-run)
  ANTHROPIC_API_KEY         Claude API key                                  (required unless --mock)
  CLAUDE_MODEL              default "claude-sonnet-5"
  NEWS_MAX_NEW              cap on new stories classified per run (default 80)
  NEWS_MIN_STORE            drop stories scored below this impact (default 30, "tight wire")
  NEWS_RETENTION_DAYS       delete events older than this (default 150; 2026-09-15, was 30 — Mete is
                            running CTE as an internal 3-month trial (mid-Sept 2026 → ~Jan 2027) and
                            wants an impact-vs-price-change correlation report at the end of it, which
                            needs every day's news docs to still exist then. 150 covers the trial
                            window plus margin. Override by setting this as a GitHub → Settings →
                            Variables entry (currently set there to 30 from the prior bump — that
                            override wins over this code default, so it must be updated too or this
                            change has no effect))
  NEWS_ALERT_IMPACT         push alert threshold (default 75; BREAKING always alerts)
  NEWS_MORNING_HOUR_VN      hour (0-23, Vietnam time, UTC+7) the daily Morning Report is generated
                            in (default 7). Any run whose Vietnam-time hour matches this generates
                            it once per calendar date — see maybe_send_morning_report().
  NEWS_STALE_HOURS          a wire (Google News) story whose real publish date, read from the
                            article page itself, is this many hours older than what Google's RSS
                            pubDate claimed is treated as a resurfaced OLD story, not breaking news
                            (default 24) — see verify_freshness().
  NEWS_VERIFY_MAX           cap on how many wire stories get their real publish date checked per
                            run (default 40) — bounds the extra fetch time/cost per run.

Usage
  python collector.py                # normal run (needs both env vars)
  python collector.py --dry-run      # collect + classify, print, do NOT write Firestore
  python collector.py --mock         # no Claude call: deterministic fake scores (for testing the page)
  python collector.py --dry-run --mock
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import feedparser
import requests

# ---------------------------------------------------------------------------
# 1. Sources
# ---------------------------------------------------------------------------

# ── Sources (31 agreed with the desk on 2026-09-11; +3 on 2026-09-15, README items 12/13/14A —
# see the "Yarın yapılacak" entries dated 14 Eylül) ──────────────────────────────────────────────
# Google News RSS searches: (query, hl, gl, ceid). Five languages: EN, RU, UK, PT-BR, ES-AR.
# The classifier reads any language and answers in English. Publisher-locked queries use
# Google's site: operator so we never have to guess a publisher's RSS address.
GOOGLE_NEWS_QUERIES = [
    # ── CROP: product, crop, weather, reports, demand ──
    ('"soybean meal" OR soymeal OR "soybean crush" OR "soybean crop"',                 'en-US', 'US', 'US:en'),
    ('"corn futures" OR "corn crop" OR "corn exports" OR "corn harvest"',               'en-US', 'US', 'US:en'),
    ('"wheat futures" OR "wheat crop" OR "wheat exports" OR "wheat harvest"',           'en-US', 'US', 'US:en'),
    ('USDA WASDE OR "crop progress" OR "export sales" OR "ending stocks" grains',       'en-US', 'US', 'US:en'),
    ('(wheat OR corn OR soybean) "crop weather" OR "weather forecast" crop',            'en-US', 'US', 'US:en'),
    ('"African swine fever" OR "hog herd" OR "pig herd" OR "feed demand" soybean meal', 'en-US', 'US', 'US:en'),
    ('"Karen Braun" OR "Andrey Sizov" OR SovEcon grain',                                'en-US', 'US', 'US:en'),
    ('"farelo de soja" OR "safra de soja" OR "exportação de soja" OR Conab',            'pt-BR', 'BR', 'BR:pt-419'),
    ('"safra de milho" OR "exportação de milho" OR "milho safrinha"',                    'pt-BR', 'BR', 'BR:pt-419'),
    ('"harina de soja" OR "cosecha de soja" OR "Bolsa de Cereales" OR Rosario soja',    'es-419', 'AR', 'AR:es-419'),
    ('"cosecha de maíz" OR "exportaciones de maíz" OR "trigo argentino"',               'es-419', 'AR', 'AR:es-419'),
    # publisher-locked (TradersEQ pool)
    ('site:barchart.com wheat OR corn OR soybean',                                      'en-US', 'US', 'US:en'),
    ('site:agweb.com grain OR corn OR soybean OR wheat',                                'en-US', 'US', 'US:en'),
    ('site:agriculture.com grain OR corn OR soybean OR wheat',                          'en-US', 'US', 'US:en'),
    ('site:agrolatam.com',                                                              'es-419', 'AR', 'AR:es-419'),
    ('site:safras.com.br',                                                              'pt-BR', 'BR', 'BR:pt-419'),
    ('site:noticiasagricolas.com.br soja OR milho OR trigo',                            'pt-BR', 'BR', 'BR:pt-419'),
    # ── WORLD: war, politics, trade policy ──
    ('"Black Sea" grain OR "grain corridor" OR "grain exports" Ukraine Russia',         'en-US', 'US', 'US:en'),
    # 2026-09-23, Mete caught this live: Reuters' "US wheat slips as traders monitor Black Sea export
    # prospects" (Zelenskiy-Trump UN meeting, bilateral energy-ceasefire talk) never reached CTE.
    # Root cause, verified against the actual headline text: the query above requires "Black Sea" next
    # to the literal word "grain" (or the exact phrases "grain corridor"/"grain exports") — this
    # headline says "Black Sea export", not "Black Sea grain" or "grain exports", so it fell through
    # every branch. A plain market-recap wire piece ("wheat slips/rises as traders watch Black Sea
    # export prospects") is exactly the kind of routine-sounding headline that misses "grain"-anchored
    # phrasing. Added a second, broader Black Sea query below that doesn't require the word "grain" at
    # all — "Black Sea" plus any of export/shipping/ceasefire/truce covers the market-recap phrasing
    # the first query's stricter wording was built for the "grain corridor" story specifically and
    # missed.
    ('"Black Sea" export OR "Black Sea" shipping OR "Black Sea" wheat OR "Black Sea" ceasefire OR "Black Sea" truce',
                                                                                         'en-US', 'US', 'US:en'),
    # 2026-09-22, Mete caught this live: Russia's Sept-22 resolution zeroing out grain export duties
    # through end-2026 was first reported by Bloomberg ~Sept 2 ("Russia Pauses Grain Export Duty
    # through the end of 2026") — CTE never carried it; none of the queries above target "duty/tax"
    # terms in English, so a non-Russian, non-site-locked story about it had nothing to match. The two
    # Russian mid-September duty cuts (16/18 Sept) WERE caught, but only via the bfm.ru/Interfax
    # site-locked queries below — a story from anywhere else (Bloomberg, Reuters, etc.) would have
    # slipped through the same way. Added explicit duty/tax queries in both languages to close this.
    ('"export duty" OR "export tax" OR "export levy" grain OR wheat OR corn OR barley Russia',
                                                                                         'en-US', 'US', 'US:en'),
    ('пошлина зерно OR пшеница OR кукуруза OR ячмень OR экспортная пошлина',            'ru', 'RU', 'RU:ru'),
    # 2026-09-22, Mete: the Houthi/Bab-el-Mandeb/Red Sea/Suez query (previously here) and the two
    # dedicated FREIGHT queries + the FreightWaves site feed (previously in SITE_FEEDS below) were
    # all removed — the desk stopped tracking freight/shipping-cost news entirely ("freight ile
    # alakalı haber ve başlıkları takipten ve değerlendirmeden çıkart"). Freight is no longer a
    # thing this collector collects, classifies or scores; only WHEAT/CORN/SBM stories are kept now
    # (see PRODUCTS below). Brent crude itself is still tracked as a pure PRICE series (PRICE_SYMBOLS
    # further down, via sync_prices()) so the page's FREIGHT tile can keep showing a Brent reference
    # line — that price sync is independent of this news pipeline.
    # 2026-09-15, README item 12: the 14 Eylül WHEAT flash-crash (VN 22:00, ZW.Z26 -14c in 5 min)
    # was very likely Trump's "Ukraine has agreed not to hit Russian Energy targets" statement —
    # a Russia-Ukraine de-escalation headline with NO grain/export keyword in it at all, so it never
    # matched anything above. Deliberately grain-free: catches the ceasefire/truce story itself, the
    # classifier's own SYMMETRY-style reasoning ("war risk premium down -> wheat BEARISH") does the
    # rest once the headline is actually collected.
    ('Russia Ukraine ceasefire OR truce OR "peace deal" OR "peace plan" OR "energy targets" OR "agreed not to strike" OR "agreed not to hit" OR de-escalation',
                                                                                         'en-US', 'US', 'US:en'),
    ('пшеница экспорт OR урожай зерна OR "зерновой коридор" OR ИКАР',                   'ru', 'RU', 'RU:ru'),
    ('соя OR "соевый шрот" OR кукуруза экспорт OR порт Новороссийск зерно',             'ru', 'RU', 'RU:ru'),
    ('експорт зерна OR пшениця OR кукурудза OR "зерновий коридор" OR порт Одеса',      'uk', 'UA', 'UA:uk'),
    ('site:interfax.com OR site:interfax.com.ua grain OR wheat OR corn',                'en-US', 'US', 'US:en'),
    ('site:agroportal.ua',                                                              'uk', 'UA', 'UA:uk'),
    ('site:bfm.ru зерно OR пшеница OR экспорт',                                         'ru', 'RU', 'RU:ru'),
]

# Direct publisher RSS (addresses taken from the desk's Inoreader) and X bridges. A dead feed is
# logged and skipped; the run never stops for one source.
SITE_FEEDS: list[tuple[str, str]] = [
    # WORLD
    ('War on the Rocks',              'https://warontherocks.com/feed/'),
    ('ISW (X)',                       'https://rss.xcancel.com/TheStudyofWar/rss'),
    # 2026-09-15, README item 14A: the 14 Eylül Trump/Ukraine-energy headline reached the desk (via
    # Mete's phone) minutes before it would have shown up as a secondary Google News article — a
    # primary-source, market-moving statement is exactly what this kind of fast X account carries
    # first. Same rss.xcancel.com bridge already used for ISW/Karen Braun/Andrey Sizov below, applied
    # to a widely-watched real-time breaking-news headline account (not grain-specific on purpose —
    # this is the "flash headline" net, the classifier + item-12/13 queries narrow it back down).
    ('Breaking Market News (X)',      'https://rss.xcancel.com/DeItaone/rss'),
    # CROP / REPORTS
    ('USDA News',                     'https://www.usda.gov/rss/latest-releases.xml'),
    ('USDA NASS',                     'https://www.nass.usda.gov/rss/reports.xml'),
    ('World Grain',                   'https://www.world-grain.com/rss/articles'),
    ('IGC via World Grain',           'https://www.world-grain.com/rss/topic/1069-igc-international-grains-council'),
    ('Karen Braun (X)',               'https://rss.xcancel.com/kannbwx/rss'),
    ('Andrey Sizov (X)',              'https://rss.xcancel.com/sizov_andre/rss'),
    # FreightWaves (dedicated freight trade press) removed 2026-09-22 along with FREIGHT tracking
    # generally — see the dated note by GOOGLE_NEWS_QUERIES above.
]

# 2026-09-17, Mete: MACRO (Brent/energy, Fed rate, dollar index, VIX, FX) dropped entirely as a
# product/category — cost-reduction request after seeing high per-run Claude token usage in the
# GitHub Actions console.
# 2026-09-22, Mete: FREIGHT dropped too, same direction taken one step further — dry-bulk rates,
# bunker fuel, canal/vessel disruption and Red Sea/Houthi/Suez coverage are no longer collected,
# classified or scored at all ("freight haberi gelmesin, freight impact doğrusu olmasın, ... Houti
# yemen gibi freight ile alakalı haber ve başlıkları takipten ve değerlendirmeden çıkart"). World
# News (POLITICS) and Commodity News (AGRI + REPORTS) now cover only WHEAT/CORN/SBM end to end. The
# 'FREIGHT' key survives in PRICE_SYMBOLS further down purely as a Brent-crude price reference for
# the page's FREIGHT tile/header ("freight header kalsin ve Brent takibi icin") — that is a plain
# price sync, unrelated to this news-product list.
PRODUCTS = ('SBM', 'CORN', 'WHEAT')
# "Tight wire": stories the classifier scores below this impact are relevant but routine (daily
# wraps, local anecdotes). They are NOT stored — the desk chose a short list over a full one.
# Classification cost is unchanged; only Firestore and the page get quieter.
NEWS_MIN_STORE = int(os.environ.get('NEWS_MIN_STORE', '30'))
CATEGORIES = ('POLITICS', 'AGRI', 'REPORTS')
EVENT_TYPES = ('WEATHER', 'SUPPLY DEMAND', 'TRADE FLOW', 'POLICY', 'GEOPOLITICS',
               'LOGISTICS', 'PRICE', 'DISEASE', 'ENERGY', 'FX', 'OTHER')

UA = 'Mozilla/5.0 (compatible; CTE-newsfeed/1.0; +https://commodity-trading-engine.web.app)'


def gnews_url(q: str, hl: str, gl: str, ceid: str) -> str:
    return f'https://news.google.com/rss/search?q={quote_plus(q)}&hl={hl}&gl={gl}&ceid={ceid}'


def fetch_feed(url: str, timeout: int = 20):
    try:
        r = requests.get(url, headers={'User-Agent': UA}, timeout=timeout)
        r.raise_for_status()
        return feedparser.parse(r.content)
    except Exception as e:  # noqa: BLE001
        print(f'  ! feed failed: {url[:90]} ({e})', file=sys.stderr)
        return None


def clean(s: str | None) -> str:
    if not s:
        return ''
    s = html.unescape(re.sub(r'<[^>]+>', ' ', s))
    return re.sub(r'\s+', ' ', s).strip()


def normalise_title(t: str) -> str:
    t = t.lower()
    t = re.sub(r'\s+-\s+[^-]{2,40}$', '', t)      # strip trailing " - Publisher" Google adds
    t = re.sub(r'[^\w\s]', '', t)
    return re.sub(r'\s+', ' ', t).strip()


def story_id(title: str, source: str, feed_kind: str) -> str:
    # Wire: Google returns the same story from many publishers — one id per HEADLINE, and the
    # extra carriers become sourceCount ("Reuters +11"). Sites: one id per headline+publisher.
    key = normalise_title(title) if feed_kind == 'wire' else f'{normalise_title(title)}|{source.lower()}'
    return hashlib.sha1(key.encode()).hexdigest()[:20]


def entry_time_ms(entry) -> int:
    for key in ('published', 'updated'):
        v = entry.get(key)
        if v:
            try:
                return int(parsedate_to_datetime(v).timestamp() * 1000)
            except Exception:  # noqa: BLE001
                pass
    for key in ('published_parsed', 'updated_parsed'):
        v = entry.get(key)
        if v:
            return int(time.mktime(v) * 1000)
    return int(time.time() * 1000)


def collect() -> dict[str, dict]:
    """Return {id: story} for every story seen this run (dedup within the run by id)."""
    stories: dict[str, dict] = {}

    def add(entry, feed_kind: str, default_source: str = ''):
        title = clean(entry.get('title'))
        if not title or len(title) < 12:
            return
        source = default_source
        if not source:
            src = entry.get('source')
            if isinstance(src, dict):
                source = clean(src.get('title'))
        if not source:
            source = clean(entry.get('author')) or 'unknown'
        # Google appends " - Publisher" to the title; drop it when we know the publisher.
        if source and title.endswith(f' - {source}'):
            title = title[: -len(source) - 3].strip()
        sid = story_id(title, source, feed_kind)
        if sid in stories:
            st = stories[sid]
            # The same article surfaces under several of our queries; that is NOT extra coverage.
            # Only a DIFFERENT publisher carrying the headline raises sourceCount.
            if source.lower() not in st['_publishers']:
                st['_publishers'].add(source.lower())
                st['sourceCount'] += 1
            st['ms'] = min(st['ms'], entry_time_ms(entry))  # first publication wins
            return
        stories[sid] = {
            'id': sid,
            'feed': feed_kind,
            'headline': title,
            'snippet': clean(entry.get('summary'))[:400],
            'url': entry.get('link') or '',
            'source': source,
            'sourceCount': 1,
            'ms': entry_time_ms(entry),
            '_publishers': {source.lower()},
        }

    # 2026-09-18, Mete: was `[:40]`. Google News RSS search is a live "current top results" snapshot,
    # not a stable chronological log — on a heavily-covered story (proven case: the Black Sea grain
    # corridor story, 17 Sept), a burst of new coverage can push an older-but-still-relevant article
    # (including from top-tier wires like the New York Times) out of the top 40 before our next run
    # (~15-20 min later) ever sees it, and it's then gone for good — no retry, no backfill. Verified
    # live: NYT's and World Grain's earliest pieces on that story never reached Firestore at all,
    # while the desk's own trader (via X, unrelated to this feed) had it ~6.5h earlier. Raised to 100
    # — Google's RSS search endpoint rarely returns much more than that per query anyway, so this
    # isn't asking for more than Google gives us; it just stops us throwing away the tail of what it
    # already returns. Classification cost is unaffected in practice: NEWS_MAX_NEW (default 80) caps
    # new-story classification per run regardless of how many raw entries we collect, and real volume
    # (2026-09-18 measurement: ~66 stored docs/day across ~80 runs/day) sits far below that ceiling —
    # this recovers currently-lost headlines, it doesn't add classification spend.
    for q, hl, gl, ceid in GOOGLE_NEWS_QUERIES:
        fp = fetch_feed(gnews_url(q, hl, gl, ceid))
        if not fp:
            continue
        for e in fp.entries[:100]:
            add(e, 'wire')
        print(f'  wire  {len(fp.entries):3d}  {q[:60]}')

    for name, url in SITE_FEEDS:
        fp = fetch_feed(url)
        if not fp:
            continue
        for e in fp.entries[:30]:
            add(e, 'sites', name)
        print(f'  sites {len(fp.entries):3d}  {name}')

    # Only stories from the last 3 days — older items would distort the trailing window.
    cutoff = int(time.time() * 1000) - 3 * 24 * 3600 * 1000
    for v in stories.values():
        v.pop('_publishers', None)
    return {k: v for k, v in stories.items() if v['ms'] >= cutoff}


# ── Freshness verification (Google News timestamp correction) ─────────────────────────────
# 2026-09-29, Mete's complaint: Google News RSS's own pubDate reflects when GOOGLE (re-)indexed
# or crawled a story, not when the article was actually first published — a genuinely days- or
# weeks-old piece can resurface in a fresh search hit with a pubDate of "just now", and nothing
# here could tell the difference: entry_time_ms() trusted that field completely, so the story
# sailed through the 3-day cutoff above as brand new. Worse, the classifier never sees `ms` at
# all (classify_with_claude() only ever sends it the headline + snippet — see below), so a
# headline that merely READS as dramatic could still be scored breaking=true / high impact and
# fire a push alert for something the desk already knew about days ago ("beni her defasinda cok
# uzuyor, eski haberleri yeni gibi cektigi icin"). This only targets wire (Google News) stories —
# the direct publisher/X-bridge feeds in SITE_FEEDS are live streams, not a re-indexed search
# snapshot, and were not the reported failure mode.
#
# Fix: for wire stories that currently LOOK fresh (within VERIFY_FRESH_HOURS — no point spending
# a fetch confirming something already reported as old), pull a bounded prefix of the article's
# own HTML and read its real publish date straight from the page (JSON-LD datePublished, OG
# article:published_time, <meta name="date">-style tags, <time datetime>) — the same metadata
# every publisher embeds for SEO and that Google's own indexer reads. If that real date is more
# than NEWS_STALE_HOURS older than what the RSS entry claimed, the story's `ms` is corrected to
# the real timestamp and the story is flagged `stale`. sanitise() then treats `stale` exactly
# like `duplicate`: impact capped to 20, breaking forced false — so a resurfaced old story falls
# out at classify_all()'s existing NEWS_MIN_STORE gate the same way a retelling of already-known
# news already does, and never reaches Firestore or a push alert.
#
# Fails open on every error (timeout, no match, non-200, anything) — a verification miss must
# never block the run or downgrade a genuinely fresh story; worst case it is scored exactly as if
# this check didn't exist. Capped at NEWS_VERIFY_MAX fetches per run so one run never spends its
# whole 10-minute GitHub Actions budget here.
NEWS_STALE_HOURS = int(os.environ.get('NEWS_STALE_HOURS', '24'))
NEWS_VERIFY_MAX = int(os.environ.get('NEWS_VERIFY_MAX', '40'))
VERIFY_FRESH_HOURS = 12        # only stories the RSS itself claims are this recent are worth checking
VERIFY_MAX_BYTES = 200_000     # publish-date metadata lives in <head>; stop well before the full page

_DATE_PATTERNS = [
    re.compile(r'"datePublished"\s*:\s*"([^"]+)"', re.IGNORECASE),
    re.compile(r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']article:published_time["\']', re.IGNORECASE),
    re.compile(r'<meta[^>]+name=["\'](?:date|publishdate|publish-date|parsely-pub-date|sailthru\.date)["\']'
               r'[^>]+content=["\']([^"\']+)["\']', re.IGNORECASE),
    re.compile(r'<meta[^>]+itemprop=["\']datePublished["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE),
    re.compile(r'<time[^>]+datetime=["\']([^"\']+)["\']', re.IGNORECASE),
]


def _parse_iso_ms(raw: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:  # noqa: BLE001
        pass
    try:
        return int(parsedate_to_datetime(raw).timestamp() * 1000)
    except Exception:  # noqa: BLE001
        return None


def fetch_true_published_ms(url: str) -> int | None:
    """Best-effort real publish date scraped from the article's own HTML metadata. None on any
    failure — the caller must treat that as 'unknown', never as 'confirmed fresh'."""
    if not url:
        return None
    r = None
    try:
        r = requests.get(url, headers={'User-Agent': UA}, timeout=8, stream=True, allow_redirects=True)
        r.raise_for_status()
        size = 0
        chunks = []
        for chunk in r.iter_content(chunk_size=8192):
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size >= VERIFY_MAX_BYTES:
                break
        text = b''.join(chunks).decode('utf-8', errors='ignore')
    except Exception:  # noqa: BLE001
        return None
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:  # noqa: BLE001
                pass
    for pat in _DATE_PATTERNS:
        m = pat.search(text)
        if m:
            ms = _parse_iso_ms(m.group(1))
            if ms:
                return ms
    return None


def verify_freshness(stories: dict[str, dict]):
    """Mutates `stories` in place: corrects `ms` and sets `stale=True` on wire stories whose real
    publish date (read from the article page) turns out to be well older than what Google News'
    RSS pubDate claimed. See the block comment above for why this exists. Never raises — any
    failure just leaves that story exactly as collect() produced it."""
    now_ms = int(time.time() * 1000)
    fresh_cut = now_ms - VERIFY_FRESH_HOURS * 3600 * 1000
    candidates = [s for s in stories.values() if s.get('feed') == 'wire' and s['ms'] >= fresh_cut]
    candidates.sort(key=lambda s: -s['ms'])   # the freshest-claiming ones are the highest false-alarm risk
    candidates = candidates[:NEWS_VERIFY_MAX]
    if not candidates:
        return
    checked = corrected = 0
    for s in candidates:
        try:
            true_ms = fetch_true_published_ms(s.get('url', ''))
        except Exception:  # noqa: BLE001
            true_ms = None
        checked += 1
        if true_ms is None:
            continue
        if s['ms'] - true_ms >= NEWS_STALE_HOURS * 3600 * 1000:
            s['ms'] = true_ms
            s['stale'] = True
            corrected += 1
    print(f'  freshness check: {checked} wire stories verified against their real publish date, '
          f'{corrected} were actually old (corrected, will be capped like a duplicate)')


# ---------------------------------------------------------------------------
# 2. Classification
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the news enricher for a commodity trading desk that IMPORTS soybean meal (SBM),
corn and wheat by sea into South-East Asia and charters dry-bulk vessels (Panamax / Supramax,
combination cargoes) from South America and the US Gulf. The desk wants ONLY news that can move
the WORLD grain complex. Small, local, anecdotal or irrelevant items must be skipped.

For every headline you receive, answer with ONE JSON object per headline inside a JSON array,
same order as the input, nothing else. Fields:

  "i"          : the input index (integer)
  "product"    : one of "SBM","CORN","WHEAT","SKIP"
                 SBM = soybeans / soybean meal / crush / soy oil complex / feed demand (hog, poultry herds)
                 2026-09-22, Mete: FREIGHT retired as a product entirely — dry-bulk rates, FFA,
                 bunker fuel, canal/port congestion, vessel supply, and Red Sea / Suez Canal /
                 Bab-el-Mandeb disruption (Houthi attacks, vessel strikes, rerouting via the Cape of
                 Good Hope) are now SKIP, full stop — the desk stopped tracking freight/shipping-cost
                 news ("freight ile alakalı haber ve başlıkları takipten ve değerlendirmeden çıkart").
                 Do not classify a Houthi/Red Sea/Suez/bunker/dry-bulk story as anything but SKIP,
                 even if it reads as market-moving for transit cost — that lane no longer exists here.
                 BLACK SEA GRAIN CORRIDOR / EXPORT-LANE stories are the one case to still classify
                 carefully: do not SKIP them just because they mention a "corridor" or shipping. A
                 story about the Black Sea grain corridor / grain export lane / grain deal
                 (Russia-Ukraine talks on safe passage, a corridor being opened, extended, suspended,
                 attacked, mined, or a port/silo/vessel struck as part of that corridor) is
                 fundamentally about how much WHEAT (or CORN, if the headline is specifically about
                 corn cargoes) reaches the world market from Russia/Ukraine — classify it as WHEAT (or
                 CORN), a supply-volume story, not a freight/transit-cost one.
                 SKIP = everything else, including former-FREIGHT stories (see above) and general
                        macro (Brent/energy moves, Fed rate, dollar index, VIX, bond yields, FX) that
                        isn't about SBM/CORN/WHEAT specifically — the desk no longer tracks a
                        standalone macro OR freight lane (2026-09-17 / 2026-09-22). SKIP ALSO when the
                        story is:
                   - about a country that does not shape world grain supply or demand (Vietnam,
                     Indonesia, Malaysia, Philippines, Guatemala, Nigeria, Bangladesh, Sudan, Iran,
                     Sri Lanka ... and similar). Countries that DO count: USA, Brazil, Argentina,
                     Russia, Ukraine, Kazakhstan, EU (France, Germany, Romania, Poland), Australia,
                     Canada, India, China, Egypt, Turkey, Pakistan and the Black Sea region.
                   - a single farm, single county/state anecdote, a local cash-bid list, a retail
                     food-price story, a company earnings or stock story
                   - gold, silver, crypto, bank/equity earnings, general stock-market moves
                   - opinion or technical-chart commentary with no new fact
  "direction"  : "BULLISH" | "BEARISH" | "NEUTRAL"
                 THIS IS A PRICE ARGUMENT, NOT SENTIMENT. BULLISH means the story argues the
                 CFR cost of that product UP (tighter supply, stronger demand, higher freight).
                 A record Brazilian crop is good news and BEARISH. An export ban is bad news and BULLISH.
                 Feed demand: herd expansion / recovery = BULLISH for SBM, disease losses / herd cuts = BEARISH.
                 BLACK SEA GRAIN CORRIDOR (product WHEAT/CORN per the rule above): the corridor being
                 opened, restored, extended, or a safe-passage/grain deal being agreed is BEARISH — it
                 means MORE Russian/Ukrainian grain reaching the world market (more supply). The corridor
                 being suspended, blocked, mined, or a vessel/port/silo tied to it being attacked is
                 BULLISH — less grain reaching the market. Do not default this to FREIGHT-style "higher
                 cost = bullish" reasoning; it is a supply-volume story like any other WHEAT/CORN item.
                 SYMMETRY RULE: crop/yield/rating headlines are two-sided. A record or rising crop/yield/
                 rating is BEARISH (more supply) — and its mirror image, a FALLING crop rating, a yield cut,
                 a downgraded harvest estimate, or a lower-than-expected production number, is BULLISH (less
                 supply). Apply this consistently in both directions: do not let "bad news for farmers"
                 framing pull a shrinking crop toward BEARISH, and do not let "good news for farmers"
                 framing pull a growing crop toward BULLISH. The price argument runs on supply tightness,
                 not on whether the news sounds good or bad for the growing country or its farmers.
                 CRUSH / PROCESSING REPORTS (NOPA and similar monthly national oilseed-processors' crush
                 data): crush volume is how many bushels of soybeans were actually PROCESSED into meal
                 and oil that month — it is a SUPPLY figure for SBM (and soyoil), not a demand figure.
                 A crush number that misses trade estimates, falls year-over-year, or hits a multi-month
                 low means LESS meal/oil reached the market than expected: that is BULLISH for SBM, the
                 direct mirror of the crop/yield SYMMETRY RULE above (less produced = tighter supply).
                 Do not read a falling or below-estimate crush print as "weak demand" and tag it BEARISH
                 — a plain report of tonnage crushed vs. trade guesses/last year, with no separate claim
                 about buyer orders or offtake, is a production/supply figure, full stop. (A headline
                 that explicitly says crush margins are weak and processors are cutting run rates because
                 buyers aren't ordering meal/oil would be a genuine demand-side story instead — but that
                 is a distinct claim the headline has to actually make, not the default reading of a
                 tonnage-vs-estimate report.)
                 MULTI-COMPONENT REPORTS (WASDE, CONAB, USDA supply/demand tables and similar): when one
                 report moves several numbers at once, net the components that matter most for world price
                 (production/yield and ending stocks outweigh minor demand-line tweaks) into ONE direction
                 for that headline, rather than picking whichever number the headline happens to lead with.
                 If two or more headlines describe the SAME underlying report or event within a short
                 window, they should normally agree on direction. Only diverge when a headline is explicitly
                 about a distinct PRICE REACTION ("markets sell off despite...", "futures slip on profit-
                 taking after...", "sell the fact") rather than the report's fundamentals — that reaction
                 headline may legitimately carry the opposite tag, but treat it as the exception, not the
                 default, and only when the headline itself frames it as a market/price reaction.
  "impact"     : 0-100 integer, how much this could move the product's WORLD price. Routine daily
                 price wraps and progress updates are 15-35. Weather/crop shocks, policy changes,
                 big tenders, port disruptions are 55-90. Reserve 90+ for genuinely market-defining events.
  "confidence" : 0-100 integer, how sure you are of direction AND impact given only the headline.
  "country"    : ISO-3166 alpha-2 of the country the story is ABOUT ("" if global).
  "category"   : the desk's reading-lane TAG shown on each headline, one of
                 "POLITICS" = government policy, tariffs, export bans/duties, war, conflict, attacks, explosions, sanctions
                 "AGRI"     = crop, harvest, weather, yield, production, planting, good/excellent ratings, ending stocks, export numbers, feed demand
                 "REPORTS"  = a scheduled/official release or data from USDA, NOAA, CONAB, Bolsa de Cereales de Buenos Aires,
                              IKAR, SovEcon, IGC, FAO, ministry statistics — the source itself is an agency or its report
                 index.html routes POLITICS into the World News box and AGRI/REPORTS into Commodity
                 News (see NEWS_LANES there) — get this field right, it decides which box the story
                 lands in.
  "breaking"   : true ONLY when the headline itself reports a discrete NEW incident, decision, or data point that
                 just happened (port attack, sudden export ban, major crop-estimate shock, big surprise tender).
                 false for a recap / explainer / "state of play" piece that summarizes an ALREADY-ONGOING situation
                 (weeks of Black Sea strikes, a running war, a season-long drought, a rerouting trend) even when the
                 headline uses dramatic wording ("halve", "crisis", "surge", "soar") — ask "does this tell me
                 something that happened today/yesterday, or is it restating/analyzing a story the desk already
                 knows about?" A piece built on analyst/consultancy data covering weeks or months (SovEcon, IGC,
                 trade-flow trackers) describing a trend is a recap, not breaking, regardless of headline tone.
                 If the piece's own body says the market/price reaction has been modest or limited so far, follow
                 that over the headline and do not score impact above 40. Routine updates are false. Expect fewer
                 than 1 in 10.
  "eventType"  : one of "WEATHER","SUPPLY DEMAND","TRADE FLOW","POLICY","GEOPOLITICS","LOGISTICS","PRICE","DISEASE","ENERGY","FX","OTHER"
  "summary"    : ONE English sentence (max 28 words) stating what happened AND why it matters
                 for the importer's cost, whatever language the headline is in.
  "duplicate"  : true if this headline reports the SAME underlying fact or data point as one of the
                 ALREADY-COVERED EVENTS listed at the end of this prompt (when that list is present) —
                 another outlet's retelling, a translation, or a slightly different number/angle on a
                 situation the desk has already been told about. This is about whether the desk already
                 knows this, not whether the wording matches. If duplicate is true, also set impact <= 20
                 regardless of how dramatic the headline sounds — restating known information is not new
                 information — and breaking must be false. If no ALREADY-COVERED EVENTS list is present,
                 or the headline adds a genuinely new fact/number/escalation the list doesn't have, false.

Use round numbers (multiples of 5) for impact and confidence. Be consistent: the same kind of
story must get the same kind of score every time."""

# The desk's own votes, fed back into every batch: a few disliked headlines say "not this kind",
# a few liked ones say "more of this". Loaded from Firestore once per run (see load_feedback).
FEEDBACK_HINT = ''


# Forcing the reply through a tool call (instead of asking the model to type out a JSON code
# block) means the API itself guarantees a parseable, schema-shaped object every time — no more
# markdown fences, commentary, or truncated/malformed JSON to regex out of free text.
CLASSIFY_TOOL = {
    'name': 'classify_headlines',
    'description': "Return the desk's classification for every headline in the batch, same order as the input.",
    'input_schema': {
        'type': 'object',
        'properties': {
            'items': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'i': {'type': 'integer', 'description': 'the input index'},
                        'product': {'type': 'string', 'enum': list(PRODUCTS) + ['SKIP']},
                        'direction': {'type': 'string', 'enum': ['BULLISH', 'BEARISH', 'NEUTRAL']},
                        'impact': {'type': 'integer'},
                        'confidence': {'type': 'integer'},
                        'country': {'type': 'string', 'description': 'ISO-3166 alpha-2, or "" if global'},
                        'category': {'type': 'string', 'enum': list(CATEGORIES)},
                        'breaking': {'type': 'boolean'},
                        'eventType': {'type': 'string', 'enum': list(EVENT_TYPES)},
                        'summary': {'type': 'string'},
                        'duplicate': {'type': 'boolean'},
                    },
                    'required': ['i', 'product', 'direction', 'impact', 'confidence',
                                 'country', 'category', 'breaking', 'eventType', 'summary', 'duplicate'],
                },
            },
        },
        'required': ['items'],
    },
}


def classify_with_claude(batch: list[dict], model: str) -> list[dict]:
    import anthropic  # local import so --mock works without the package configured

    client = anthropic.Anthropic()
    lines = []
    for i, s in enumerate(batch):
        snippet = f' | {s["snippet"][:200]}' if s.get('snippet') else ''
        lines.append(f'{i}. [{s["source"]}] {s["headline"]}{snippet}')
    user = 'Headlines:\n' + '\n'.join(lines) + '\n\nClassify every headline above by calling classify_headlines.'

    # No `temperature`: the 1.x SDK dropped it. Determinism comes from the prompt's
    # "same kind of story → same kind of score" rule and the round-number instruction.
    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        system=SYSTEM_PROMPT + FEEDBACK_HINT + KNOWN_EVENTS_HINT,
        tools=[CLASSIFY_TOOL],
        tool_choice={'type': 'tool', 'name': 'classify_headlines'},
        messages=[{'role': 'user', 'content': user}],
    )
    for block in resp.content:
        if getattr(block, 'type', '') == 'tool_use' and block.name == 'classify_headlines':
            items = block.input.get('items')
            if isinstance(items, list):
                return items
    raise ValueError('model did not call classify_headlines: ' + repr(resp.stop_reason))


def classify_mock(batch: list[dict]) -> list[dict]:
    """Deterministic fake scores derived from the id — for testing the page without spending tokens."""
    out = []
    for i, s in enumerate(batch):
        h = int(s['id'][:8], 16)
        prod = PRODUCTS[h % len(PRODUCTS)]
        direction = ('BULLISH', 'BEARISH', 'NEUTRAL')[(h >> 4) % 3]
        out.append({
            'i': i, 'product': prod, 'direction': direction,
            'impact': 30 + 5 * ((h >> 8) % 13), 'confidence': 40 + 5 * ((h >> 12) % 11),
            'country': ('US', 'BR', 'AR', 'VN', 'CN', '')[(h >> 16) % 6],
            'category': CATEGORIES[(h >> 18) % len(CATEGORIES)],
            'breaking': (h >> 22) % 10 == 0,
            'eventType': EVENT_TYPES[(h >> 20) % len(EVENT_TYPES)],
            'summary': f'[mock] {s["headline"][:80]}',
            'duplicate': False,
        })
    return out


def sanitise(ans: dict, story: dict) -> dict | None:
    prod = str(ans.get('product', 'SKIP')).upper()
    if prod not in PRODUCTS:
        return None
    direction = str(ans.get('direction', 'NEUTRAL')).upper()
    if direction not in ('BULLISH', 'BEARISH', 'NEUTRAL'):
        direction = 'NEUTRAL'
    def clamp(v):
        try:
            return max(0, min(100, int(round(float(v)))))
        except Exception:  # noqa: BLE001
            return 0
    et = str(ans.get('eventType', 'OTHER')).upper()
    if et not in EVENT_TYPES:
        et = 'OTHER'
    cat = str(ans.get('category', '')).upper()
    if cat not in CATEGORIES:
        # 2026-09-17: MACRO category retired along with the MACRO product; 2026-09-22: FREIGHT
        # retired too (see PRODUCTS above) — any story that didn't come back with a valid category
        # falls back to AGRI, the desk's general commodity-fundamentals lane.
        cat = 'AGRI'
    breaking = ans.get('breaking')
    breaking = breaking is True or str(breaking).lower() == 'true'
    duplicate = ans.get('duplicate')
    duplicate = duplicate is True or str(duplicate).lower() == 'true'
    # 2026-09-29: verify_freshness() flags a wire story whose real publish date (read from the
    # article page) turned out to be well older than Google News' own RSS pubDate claimed. The
    # classifier never sees `ms` at all — it scored breaking/impact from the headline text alone —
    # so a resurfaced old story gets the exact same treatment as a `duplicate` below.
    stale = bool(story.get('stale'))
    impact = clamp(ans.get('impact'))
    if duplicate or stale:
        # 2026-09-16 (duplicate) / 2026-09-29 (stale): neither is new information to the desk — cap
        # it below NEWS_MIN_STORE (30) so it's dropped at the existing "tight wire" gate in
        # classify_all() and never reaches Firestore, same as any other routine story. Enforced here
        # in code rather than trusted to the model's own impact number, which can still run high for
        # a dramatic-sounding duplicate/stale headline.
        impact = min(impact, 20)
        breaking = False
    return {
        **{k: story[k] for k in ('id', 'feed', 'headline', 'url', 'source', 'sourceCount', 'ms')},
        'product': prod,
        'direction': direction,
        'impact': impact,
        'confidence': clamp(ans.get('confidence')),
        'country': str(ans.get('country', '') or '')[:2].upper(),
        'category': cat,
        'breaking': breaking,
        'eventType': et,
        'summary': str(ans.get('summary', '') or '')[:300],
        'duplicate': duplicate,
        'stale': stale,
        # trend = how many publishers carry this EVENT. Starts at the number of outlets Google
        # returned for this exact headline; clustering (below) adds differently-worded reports.
        'trend': int(story.get('sourceCount', 1)),
        'sources': [story['source']],
        'classifiedAt': int(time.time() * 1000),
    }


def classify_all(stories: list[dict], mock: bool, model: str, batch_size: int = 12) -> tuple[list[dict], set[str]]:
    """Returns (kept docs, ids of every story the model actually answered for).
    A batch that fails (API error, bad JSON) is NOT counted as processed, so those stories are
    retried next run instead of being silently marked as seen."""
    results: list[dict] = []
    processed: set[str] = set()
    dropped_low = 0
    for start in range(0, len(stories), batch_size):
        batch = stories[start:start + batch_size]
        try:
            answers = classify_mock(batch) if mock else classify_with_claude(batch, model)
        except Exception as e:  # noqa: BLE001
            print(f'  ! classify batch failed: {e}', file=sys.stderr)
            continue
        processed.update(s['id'] for s in batch)
        by_i = {int(a.get('i', -1)): a for a in answers if isinstance(a, dict)}
        for i, s in enumerate(batch):
            a = by_i.get(i)
            if not a:
                continue
            doc = sanitise(a, s)
            if doc is None:
                continue
            if doc['impact'] < NEWS_MIN_STORE:
                dropped_low += 1
                continue
            results.append(doc)
        print(f'  classified {min(start + batch_size, len(stories))}/{len(stories)}')
    if dropped_low:
        print(f'  {dropped_low} routine stories dropped (impact < {NEWS_MIN_STORE})')
    return results, processed


# ---------------------------------------------------------------------------
# 3. Firestore
# ---------------------------------------------------------------------------

def firestore_client():
    import firebase_admin
    from firebase_admin import credentials, firestore

    raw = os.environ.get('FIREBASE_SERVICE_ACCOUNT')
    if not raw:
        raise SystemExit('FIREBASE_SERVICE_ACCOUNT env var missing (paste the service-account JSON).')
    cred = credentials.Certificate(json.loads(raw))
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    return firestore.client()


# ── Seen-id cache ───────────────────────────────────────────────────────────
# GitHub Actions restores this file from its cache before each run and saves it after, so
# "have we stored this story already?" is answered locally for free. Firestore is only asked
# about the handful of ids the cache has never seen, which keeps daily reads in the hundreds
# instead of the tens of thousands. Entries older than CACHE_DAYS are dropped.

CACHE_PATH = os.environ.get('NEWS_CACHE_PATH', os.path.join(os.path.dirname(__file__), '.cache', 'seen_ids.json'))
CACHE_DAYS = 7          # how long a seen id is remembered
EVENT_DAYS = 3          # how long a stored event stays eligible for clustering


def load_cache() -> dict:
    """Returns {'seen': {id: ms}, 'events': [event, …]}; tolerant of the older flat {id: ms} file."""
    now_ms = int(time.time() * 1000)
    try:
        with open(CACHE_PATH, encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return {'seen': {}, 'events': []}
    except Exception as e:  # noqa: BLE001
        print(f'  ! cache unreadable, starting fresh ({e})', file=sys.stderr)
        return {'seen': {}, 'events': []}
    if 'seen' not in data:                       # legacy flat format
        data = {'seen': data, 'events': []}
    seen_cut = now_ms - CACHE_DAYS * 24 * 3600 * 1000
    ev_cut = now_ms - EVENT_DAYS * 24 * 3600 * 1000
    return {
        'seen': {k: v for k, v in data.get('seen', {}).items() if isinstance(v, int) and v >= seen_cut},
        'events': [e for e in data.get('events', []) if isinstance(e, dict) and e.get('ms', 0) >= ev_cut],
    }


def save_cache(cache: dict):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False)


# ── Event clustering ────────────────────────────────────────────────────────
# The same event reaches the wire under many headlines, in several languages. Google already
# folds IDENTICAL headlines (sourceCount); this step folds differently-worded reports of the
# same event. The English summary the classifier writes is the common ground: two reports of
# the Novorossiysk fire share "novorossiysk port drone attack fire wheat export" whatever
# language they were written in. A match must also agree on product, direction and country,
# and sit within CLUSTER_HOURS of each other.
#
# Outcome: ONE document per event carries the impact; `trend` counts the publishers
# (identical-headline outlets + clustered reports) and `sources` lists them.

CLUSTER_HOURS = 36
CLUSTER_OVERLAP = 0.50
_STOP = set('''the a an and or of to in on for by with as at from is are was were be been this that these those it its
into over under after before amid while than then also more most less new says said say report reports
could would may might will can up down out about per vs
export exports exporting exporter exporters import imports importing importer importers supply supplies demand
price prices market markets grain grains crop crops trade trading shipment shipments
wheat corn maize soybean soybeans soy soymeal meal sbm freight
key main major big large ample tight amid seen hint hints hinting implies implying pointing adding adds
threat threatens threatening risk risks routine update signal signals'''.split())
# 2026-09-16, Mete: SBM net impact showed -140 (bearish) on 2026-09-15 while the actual futures price
# rose. Traced it to Firestore: three separate qualifying SBM docs that day were all wire pickups of
# the SAME CONAB press release (Brazil's 2025/26 season final tally, "361.7 million tons, a record"),
# published 1-11 minutes apart in Portuguese by three different outlets — "Safra de grãos 2025/26 é
# estimada em 361,7 milhões de toneladas pela Conab" / "Safra 2025/26 termina com colheita recorde,
# aponta Conab" / "Conab eleva previsão de safra total de grãos do Brasil para recorde de 361,7 mi t
# em 2025/26". cluster() below is supposed to merge exactly this case into one event, but didn't —
# so one underlying fact got counted (and weighted) three times instead of once. Root cause was in
# event_tokens(): `re.findall(r'[a-z0-9]{3,}', text)` is ASCII-only, so it silently mangles accented
# Portuguese/Spanish words instead of matching them whole — "grãos" splits into "gr"+"os" (both under
# the 3-char minimum, so the word vanishes entirely), "milhões" survives only as "milh", "previsão"
# only as "previs". Those are exactly the high-signal words that would have proven the three articles
# were the same story, so the overlap-coefficient check in same_event() came in under CLUSTER_OVERLAP
# for a case a human reads as obviously identical. Separately, the stopword list below is English-only,
# so Portuguese/Spanish connector words ("pela", "com", "para", "aponta", "eleva", "termina", ...) were
# passing through as if they were content tokens, diluting the ratio further. Fixed both: `\w` (Unicode
# mode, Python 3 default) matches accented letters as part of the same word instead of splitting on
# them, and a small PT/ES connector list was added alongside the English one. This does NOT touch
# CLUSTER_OVERLAP itself or the SYMMETRY RULE (a record harvest being called BEARISH is still correct
# in principle) — it only fixes the dedup miss that let one data point count three times.
_STOP = _STOP | set('''de da do das dos em no na nos nas por para pelo pela com uma um umas uns que como
mais menos após ante sobre aponta eleva termina segundo conforme entre desde até sem seu sua seus suas
este esta estes estas isso esse essa isto e ou mas se foi ser tem têm sao são'''.split())


def event_tokens(doc: dict) -> list[str]:
    text = f"{doc.get('summary', '')} {doc.get('headline', '')}".lower()
    words = re.findall(r'\w{3,}', text, flags=re.UNICODE)
    return sorted({w for w in words if w not in _STOP})


def same_event(ev: dict, doc: dict, doc_tokens: set[str]) -> bool:
    if ev.get('product') != doc['product'] or ev.get('direction') != doc['direction']:
        return False
    if ev.get('country') and doc.get('country') and ev['country'] != doc['country']:
        return False
    if abs(ev.get('ms', 0) - doc['ms']) > CLUSTER_HOURS * 3600 * 1000:
        return False
    et = set(ev.get('tokens', []))
    if len(et) < 3 or len(doc_tokens) < 3:
        return False
    inter = len(et & doc_tokens)
    # Overlap coefficient (shared / smaller set): robust to one report being much longer than
    # the other. Summaries are ~15 content words; two reports of one event share half of them,
    # two different stories about the same product share one or two.
    return inter / min(len(et), len(doc_tokens)) >= CLUSTER_OVERLAP


def cluster(docs: list[dict], events: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    """Returns (new_head_docs, updates_for_existing_heads{id: {'trend','sources'}}).
    Mutates `events` (appends new heads, bumps matched ones)."""
    new_heads: list[dict] = []
    updates: dict[str, dict] = {}
    this_run: dict[str, dict] = {}
    for doc in sorted(docs, key=lambda d: d['ms']):
        toks = set(event_tokens(doc))
        match = next((e for e in events if same_event(e, doc, toks)), None)
        if match is None:
            events.append({
                'id': doc['id'], 'ms': doc['ms'], 'product': doc['product'], 'direction': doc['direction'],
                'country': doc.get('country', ''), 'tokens': sorted(toks),
                'trend': doc['trend'], 'sources': list(doc['sources']),
                'summary': doc.get('summary', ''),  # 2026-09-16: kept so known_events_hint() can show the
                                                     # desk a readable one-liner, not just a token bag — see below.
            })
            new_heads.append(doc)
            this_run[doc['id']] = doc
            continue
        match['trend'] = int(match.get('trend', 1)) + int(doc['trend'])
        match['tokens'] = sorted(set(match.get('tokens', [])) | toks)   # the event's vocabulary grows with each report
        for s in doc['sources']:
            if s not in match.setdefault('sources', []):
                match['sources'].append(s)
        head = this_run.get(match['id'])
        if head is not None:                       # head created in this run: fold in before writing
            head['trend'] = match['trend']
            head['sources'] = list(match['sources'])
        else:                                      # head already in Firestore from an earlier run
            updates[match['id']] = {'trend': match['trend'], 'sources': list(match['sources'])}
    return new_heads, updates


# 2026-09-16, Mete: cluster()/same_event() above catches near-verbatim wire pickups of ONE press
# release (the SBM/CONAB fix earlier today) — but it missed a different, very common case: several
# outlets independently WRITING UP the same underlying situation in their own words (or in Russian)
# over a few hours. Example that triggered this: "Russia Reroutes Grain Exports as Black Sea Strikes
# Halve Shipments" (Global Agriculture), "Black Sea disruptions fuel rally in corn, wheat markets"
# (Wisconsin Farmer), and a Russian-language SovEcon pickup — three sources, same underlying fact
# (Russian wheat exports down on Black Sea strikes), 20-40 minutes apart, each scored BULLISH 65-75
# by the classifier as if it were fresh. Measured the token overlap between these three: 0.10-0.33,
# nowhere near CLUSTER_OVERLAP (0.50) — a paraphrase in different words, and especially a different
# LANGUAGE headline, just doesn't share enough literal tokens with an English one, no matter the
# threshold. Lowering CLUSTER_OVERLAP to catch this would risk merging genuinely distinct stories
# that happen to share a few generic words. Token overlap is the wrong tool for "is this the same
# underlying fact, retold" — that's a semantic judgment, which is exactly what the classifier model
# is good at and the token-bag heuristic isn't.
# So instead of another cluster() tweak, the classifier itself now gets a short briefing of what the
# desk has ALREADY been told in the last DEDUP_HOURS (below), built from the same `events` cache
# cluster() already maintains, and returns a new `duplicate` field per headline (see CLASSIFY_TOOL /
# SYSTEM_PROMPT). A duplicate is capped to impact<=20 in sanitise(), which puts it under
# NEWS_MIN_STORE (30) — so a re-telling of an already-known story is dropped at the same "tight
# wire" gate as any routine update, and never reaches Firestore or the desk at all, same as Mete
# asked for ("bize dusmesi bile sacma").
# Cost note: this text rides along on every classify_with_claude() call for the rest of THIS run
# (same pattern as FEEDBACK_HINT below), so it's kept deliberately small — a 24h window and at most
# DEDUP_MAX one-line summaries, not the full 3-day EVENT_DAYS cache.
DEDUP_HOURS = 24
DEDUP_MAX = 18
KNOWN_EVENTS_HINT = ''


def known_events_hint(events: list[dict]) -> str:
    global KNOWN_EVENTS_HINT
    cutoff = int(time.time() * 1000) - DEDUP_HOURS * 3600 * 1000
    recent = sorted((e for e in events if e.get('ms', 0) >= cutoff and e.get('summary')),
                     key=lambda e: -e['ms'])[:DEDUP_MAX]
    if not recent:
        KNOWN_EVENTS_HINT = ''
        return KNOWN_EVENTS_HINT
    lines = [f"  - {e['product']} {e['direction']}: {e['summary'][:130]}" for e in recent]
    KNOWN_EVENTS_HINT = (
        f"\n\nALREADY-COVERED EVENTS the desk has seen in the last {DEDUP_HOURS}h — if a headline below "
        "reports the same underlying fact/data point as one of these (same situation retold by another "
        "outlet, in another language, or with a marginally different number/angle), set duplicate=true "
        "for it:\n" + '\n'.join(lines)
    )
    return KNOWN_EVENTS_HINT


def existing_ids(db, ids: list[str]) -> set[str]:
    found: set[str] = set()
    col = db.collection('news')
    for start in range(0, len(ids), 300):
        refs = [col.document(i) for i in ids[start:start + 300]]
        for snap in db.get_all(refs):
            if snap.exists:
                found.add(snap.id)
    return found


def write_docs(db, docs: list[dict], updates: dict[str, dict]):
    col = db.collection('news')
    ops = [('set', d['id'], d) for d in docs] + [('merge', i, u) for i, u in updates.items()]
    for start in range(0, len(ops), 400):
        batch = db.batch()
        for kind, doc_id, payload in ops[start:start + 400]:
            if kind == 'set':
                batch.set(col.document(doc_id), payload)
            else:
                batch.set(col.document(doc_id), payload, merge=True)
        batch.commit()
    # heartbeat so the page can show "last update"
    db.collection('news_meta').document('status').set({
        'lastRunMs': int(time.time() * 1000),
        'lastRunIso': datetime.now(timezone.utc).isoformat(),
        'written': len(docs),
        'clustered': len(updates),
    })


# ---------------------------------------------------------------------------

# ── Desk feedback (👍 / 👎 on the page) ─────────────────────────────────────
# Documents in `news_feedback/{storyId}`: {vote: 'like'|'dislike', ms, headline, source, product,
# category, direction, impact}. Two effects:
#   1. The most recent votes are fed to the classifier as examples (FEEDBACK_HINT).
#   2. A source that collects MUTE_AFTER dislikes is muted: its stories are dropped before
#      classification (no cost) and the list is written to news_meta/muted so the page can show it.

FEEDBACK_DAYS = 30
FEEDBACK_EXAMPLES = 12       # per side, in the prompt
MUTE_AFTER = 5


def load_feedback(db) -> tuple[str, set[str]]:
    """Returns (prompt hint, muted sources)."""
    global FEEDBACK_HINT
    cutoff = int(time.time() * 1000) - FEEDBACK_DAYS * 24 * 3600 * 1000
    likes, dislikes = [], []
    per_source: dict[str, int] = {}
    try:
        for snap in db.collection('news_feedback').where('ms', '>=', cutoff).stream():
            d = snap.to_dict() or {}
            vote = d.get('vote')
            head = str(d.get('headline', ''))[:110]
            if not head:
                continue
            if vote == 'like':
                likes.append((d.get('ms', 0), head))
            elif vote == 'dislike':
                dislikes.append((d.get('ms', 0), head))
                src = str(d.get('source', '')).strip().lower()
                if src:
                    per_source[src] = per_source.get(src, 0) + 1
    except Exception as e:  # noqa: BLE001
        print(f'  ! feedback unreadable ({e}); continuing without it', file=sys.stderr)
        return '', set()
    likes.sort(reverse=True)
    dislikes.sort(reverse=True)
    parts = []
    if dislikes:
        parts.append('The desk marked these headlines as NOT WANTED — score similar stories as SKIP or below 30:\n' +
                     '\n'.join('  - ' + h for _, h in dislikes[:FEEDBACK_EXAMPLES]))
    if likes:
        parts.append('The desk marked these headlines as EXACTLY what it wants — treat similar stories as relevant:\n' +
                     '\n'.join('  - ' + h for _, h in likes[:FEEDBACK_EXAMPLES]))
    FEEDBACK_HINT = ('\n\n' + '\n\n'.join(parts)) if parts else ''
    muted = {src for src, n in per_source.items() if n >= MUTE_AFTER}
    print(f'  feedback: {len(likes)} likes, {len(dislikes)} dislikes, {len(muted)} muted sources')
    return FEEDBACK_HINT, muted


def write_muted(db, muted: set[str]):
    try:
        db.collection('news_meta').document('muted').set({'sources': sorted(muted), 'ms': int(time.time() * 1000)})
    except Exception as e:  # noqa: BLE001
        print(f'  ! could not write muted list ({e})', file=sys.stderr)


# ── Retention: 30 days (2026-09-14, was 10 — Mete wanted a full month of history to line up
# against the CBOT price charts). The History snapshot keeps each day's reading regardless. ─────
RETENTION_DAYS = int(os.environ.get('NEWS_RETENTION_DAYS', '150'))


def prune_old(db) -> int:
    cutoff = int(time.time() * 1000) - RETENTION_DAYS * 24 * 3600 * 1000
    deleted = 0
    try:
        col = db.collection('news')
        while True:
            snaps = list(col.where('ms', '<', cutoff).limit(300).stream())
            if not snaps:
                break
            batch = db.batch()
            for sn in snaps:
                batch.delete(sn.reference)
            batch.commit()
            deleted += len(snaps)
            if len(snaps) < 300:
                break
    except Exception as e:  # noqa: BLE001
        print(f'  ! prune failed ({e})', file=sys.stderr)
    return deleted


# ── Push alerts (Firebase Cloud Messaging) ──────────────────────────────────
# The page registers each phone/browser in `news_devices/{token}`. Every new event that is
# BREAKING or scores >= ALERT_IMPACT is pushed to all of them. Dead tokens are removed.
ALERT_IMPACT = int(os.environ.get('NEWS_ALERT_IMPACT', '75'))


def push_alerts(db, heads: list[dict]) -> int:
    alerts = [h for h in heads if h.get('breaking') or h.get('impact', 0) >= ALERT_IMPACT]
    if not alerts:
        return 0
    try:
        from firebase_admin import messaging
        tokens = [sn.id for sn in db.collection('news_devices').stream()]
    except Exception as e:  # noqa: BLE001
        print(f'  ! devices unreadable ({e})', file=sys.stderr)
        return 0
    if not tokens:
        print('  alerts: no registered devices')
        return 0
    sent = 0
    dead: set[str] = set()
    for h in sorted(alerts, key=lambda d: -d['impact'])[:5]:          # never spam more than 5 per run
        arrow = '▲' if h['direction'] == 'BULLISH' else '▼' if h['direction'] == 'BEARISH' else '•'
        title = f"{'BREAKING · ' if h.get('breaking') else ''}{h['product']} {arrow} {h['impact']}"
        body = (h.get('summary') or h['headline'])[:180]
        msg = messaging.MulticastMessage(
            tokens=tokens,
            notification=messaging.Notification(title=title, body=body),
            webpush=messaging.WebpushConfig(
                notification=messaging.WebpushNotification(title=title, body=body, icon='/icon-192.png', tag=h['id']),
                fcm_options=messaging.WebpushFCMOptions(link='https://commodity-trading-engine.web.app/#news'),
            ),
            data={'id': h['id'], 'url': h.get('url', ''), 'category': h.get('category', '')},
        )
        try:
            resp = messaging.send_each_for_multicast(msg)
            sent += resp.success_count
            for tok, r in zip(tokens, resp.responses):
                if not r.success and r.exception is not None:
                    code = getattr(getattr(r.exception, 'code', None), 'name', '') or str(r.exception)
                    if 'UNREGISTERED' in code.upper() or 'NOT_FOUND' in code.upper() or 'INVALID' in code.upper():
                        dead.add(tok)
        except Exception as e:  # noqa: BLE001
            print(f'  ! push failed ({e})', file=sys.stderr)
    for tok in dead:
        try:
            db.collection('news_devices').document(tok).delete()
        except Exception:  # noqa: BLE001
            pass
    print(f'  alerts: {len(alerts)} qualifying, {sent} deliveries, {len(dead)} dead tokens removed')
    return sent


# ── Price history (for the site's merged net-impact + futures price chart) ────────────────
# 2026-09-14, Mete's request: the trailing net-impact line and the CBOT futures price should be
# drawn on the SAME chart, with a real price axis and a hover tooltip. That's only possible if WE
# hold the actual price numbers — a TradingView iframe embed can't be read from outside itself, and
# there's no way to draw our own line into it. Yahoo Finance's public chart endpoint (the same one
# the popular `yfinance` Python package wraps) gives free, keyless daily OHLC for CBOT/ICE futures
# continuous front-month contracts — fine for a daily-bar line chart, not meant for tick trading.
# MACRO (formerly its own product, with separate DXY/VIX TradingView widgets on the page) was
# removed entirely 2026-09-17 — nothing here to update for it, this dict just never had it.
#
# NOT independently verified reachable from GitHub Actions as of this writing — this sandbox's own
# network policy blocks both this endpoint and its usual fallback (stooq.com) for unrelated reasons
# (an egress allowlist, and a tool that honors robots.txt), so this was written against Yahoo's
# well-documented, widely-used (via `yfinance`) response shape rather than a live test from here.
# First real run should be checked (see `sync_prices` logging below, and Firestore `prices/*`).
PRICE_SYMBOLS = {
    'WHEAT':   'ZW=F',   # CBOT Wheat, continuous front-month
    'CORN':    'ZC=F',   # CBOT Corn
    'SBM':     'ZM=F',   # CBOT Soybean Meal
    # 2026-09-22, Mete: FREIGHT was retired as a NEWS product (see PRODUCTS above) — no more freight
    # headlines are collected, classified or scored — but this price entry stays on purpose. It's
    # what still feeds the page's FREIGHT tile/header with a pure Brent-crude reference line ("freight
    # header kalsin ve Brent takibi icin ama impact yok"), independent of the news pipeline:
    # sync_prices() below loops over this dict directly, not over PRODUCTS, so it keeps refreshing.
    'FREIGHT': 'BZ=F',   # ICE Brent Crude
}
PRICE_REFRESH_HOURS = 4    # don't re-fetch the 5y DAILY series more often than this — daily bars
                           # barely move intraday anyway, keeps us a polite, low-volume caller for that
                           # part. Does NOT gate the intraday series below — that one is meant to look
                           # live, so it refreshes on every run (still cheap: ~2 days of 5m bars).
PRICE_YEARS = 5

# 2026-09-14 addition: the "Daily" chart range used to be the TradingView widget's own live intraday
# view before the chart merge — Mete noticed the merged chart lost that ("daily'de canli anlik grafik
# olmasi lazim"). A single point per day (the DAILY series above) can't show that, so Daily gets its
# own short intraday series instead: 5-minute bars, refreshed every collector run (~15 min), so the
# page's "Daily" selection actually tracks the live session instead of one flat point.
INTRADAY_RANGE = '2d'      # 2 days of buffer so "today" still has bars right after a weekend/holiday
INTRADAY_INTERVAL = '5m'


def fetch_price_series(symbol: str) -> list[dict] | None:
    """Daily closes for `symbol` over the last PRICE_YEARS, oldest first: [{'t': ms, 'c': close}, …].
    Returns None on any failure — the caller must treat that as "try again next run", not fatal."""
    return _fetch_yahoo_chart(symbol, range_=f'{PRICE_YEARS}y', interval='1d')


def fetch_intraday_series(symbol: str) -> list[dict] | None:
    """5-minute bars for `symbol` over the last ~2 days, oldest first: [{'t': ms, 'c': close}, …].
    Used only for the chart's "Daily" range so it shows real live-session movement instead of a
    single flat daily-close point. Returns None on any failure — never fatal to the run."""
    return _fetch_yahoo_chart(symbol, range_=INTRADAY_RANGE, interval=INTRADAY_INTERVAL)


def _fetch_yahoo_chart(symbol: str, range_: str, interval: str) -> list[dict] | None:
    url = f'https://query2.finance.yahoo.com/v8/finance/chart/{symbol}'
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; CTE-newsfeed/1.0; +https://commodity-trading-engine.web.app/)'}
    try:
        r = requests.get(url, params={'range': range_, 'interval': interval}, headers=headers, timeout=20)
        r.raise_for_status()
        result = r.json()['chart']['result'][0]
        timestamps = result['timestamp']
        closes = result['indicators']['quote'][0]['close']
        bars = [{'t': int(ts) * 1000, 'c': round(c, 4)} for ts, c in zip(timestamps, closes) if c is not None]
        return bars or None
    except Exception as e:  # noqa: BLE001
        print(f'  ! price fetch failed for {symbol} ({range_}/{interval}) ({e})', file=sys.stderr)
        return None


def sync_prices(db):
    """Refreshes Firestore `prices/{product}`: the 5y DAILY series when missing/older than
    PRICE_REFRESH_HOURS, and the short INTRADAY series on every run (see INTRADAY_RANGE comment
    above). Writes with merge=True so an intraday-only update doesn't clobber the daily series (and
    vice versa). Never raises — a price-data outage must not break the news run (the page falls back
    gracefully to no-price-line if a field is missing or stale, see index.html)."""
    now_ms = int(time.time() * 1000)
    stale_before = now_ms - PRICE_REFRESH_HOURS * 3600 * 1000
    daily_updated = 0
    intraday_updated = 0
    for product, symbol in PRICE_SYMBOLS.items():
        try:
            ref = db.collection('prices').document(product)
            snap = ref.get()
            existing = snap.to_dict() if snap.exists else {}
            payload = {}

            if existing.get('updatedMs', 0) <= stale_before:
                bars = fetch_price_series(symbol)
                if bars:
                    payload['bars'] = bars
                    payload['updatedMs'] = now_ms
                    daily_updated += 1

            intraday = fetch_intraday_series(symbol)
            if intraday:
                payload['intraday'] = intraday
                payload['intradayUpdatedMs'] = now_ms
                intraday_updated += 1

            if payload:
                payload['symbol'] = symbol
                ref.set(payload, merge=True)
        except Exception as e:  # noqa: BLE001
            print(f'  ! price sync failed for {product} ({e})', file=sys.stderr)
    if daily_updated or intraday_updated:
        print(f'  prices: refreshed {daily_updated}/{len(PRICE_SYMBOLS)} daily, '
              f'{intraday_updated}/{len(PRICE_SYMBOLS)} intraday')
    return daily_updated


# ── Morning report (VN 07:00 daily summary) ─────────────────────────────────
# 2026-09-15, Mete's request (README item 15): every morning around Vietnam 07:00, a summary of the
# previous 24h — overnight qualifying headlines per product, and how far each CBOT future actually
# moved. GitHub Actions already runs every ~15 min (cron-job.org), so this needs no schedule of its
# own: every run just checks "is it currently the target Vietnam hour, and has today's report
# already been written?" — idempotent against the exact trigger timing, safe to call on every run.
VN_OFFSET_HOURS = 7                                                    # Vietnam is UTC+7, no DST
MORNING_REPORT_HOUR = int(os.environ.get('NEWS_MORNING_HOUR_VN', '7')) # generate once per VN date
MORNING_MIN_IMPACT = 40    # mirrors index.html's NEWS_MIN_IMPACT (2026-09-16: 50 -> 45; 2026-09-22:
MORNING_MIN_CONF = 50      # 45 -> 40, Mete's call to count impact-40 stories into the chart/tiles
                            # too) — "qualifying" must mean the same thing here as it does on the
                            # page, or the two would quietly disagree.
MORNING_WINDOW_MS = 24 * 3600 * 1000


def _vn_now():
    from datetime import timedelta
    return datetime.now(timezone.utc) + timedelta(hours=VN_OFFSET_HOURS)


def _price_pct_change(rec: dict | None, window_ms: int, now_ms: int) -> float | None:
    """% change of the freshest close vs. the closest bar at/before (now_ms - window_ms). Prefers the
    intraday (5-min) series — freshest — and falls back to the daily series. None if neither works."""
    if not rec:
        return None
    for key in ('intraday', 'bars'):
        series = rec.get(key)
        if not isinstance(series, list) or len(series) < 2:
            continue
        series = sorted(series, key=lambda b: b['t'])
        last = series[-1]
        if now_ms - last['t'] > MORNING_WINDOW_MS:       # feed is stale — don't report a fake move
            continue
        base = None
        for b in reversed(series):
            if b['t'] <= last['t'] - window_ms:
                base = b
                break
        if base is None or not base.get('c'):
            continue
        return round((last['c'] - base['c']) / base['c'] * 100, 2)
    return None


def build_morning_report(db) -> dict | None:
    """Reads the last 24h of Firestore `news` + `prices/*` and returns the report dict, or None on
    a read failure (the caller treats that as 'try again next run', never fatal)."""
    now_ms = int(time.time() * 1000)
    window_start = now_ms - MORNING_WINDOW_MS
    try:
        snaps = list(db.collection('news').where('ms', '>=', window_start).stream())
    except Exception as e:  # noqa: BLE001
        print(f'  ! morning report: could not read news ({e})', file=sys.stderr)
        return None
    docs = [d for d in (sn.to_dict() for sn in snaps) if d]
    products = {}
    for p in PRODUCTS:
        qual = [d for d in docs if d.get('product') == p and d.get('impact', 0) >= MORNING_MIN_IMPACT
                and d.get('confidence', 0) >= MORNING_MIN_CONF]
        net = sum((d['impact'] if d.get('direction') == 'BULLISH'
                    else -d['impact'] if d.get('direction') == 'BEARISH' else 0) for d in qual)
        price_pct = None
        if p in PRICE_SYMBOLS:
            try:
                snap = db.collection('prices').document(p).get()
                price_pct = _price_pct_change(snap.to_dict() if snap.exists else None, MORNING_WINDOW_MS, now_ms)
            except Exception:  # noqa: BLE001
                price_pct = None
        products[p] = {
            'net': int(net),
            'bull': sum(1 for d in qual if d.get('direction') == 'BULLISH'),
            'bear': sum(1 for d in qual if d.get('direction') == 'BEARISH'),
            'n': len(qual),
            'pricePct': price_pct,
        }
    top = sorted(docs, key=lambda d: -d.get('impact', 0))[:6]
    return {
        'dateVN': _vn_now().strftime('%Y-%m-%d'),
        'generatedMs': now_ms,
        'windowStartMs': window_start,
        'windowEndMs': now_ms,
        'products': products,
        'top': [{'headline': d.get('headline', ''), 'product': d.get('product', ''),
                 'direction': d.get('direction', ''), 'impact': d.get('impact', 0),
                 'breaking': bool(d.get('breaking')), 'url': d.get('url', ''),
                 'source': d.get('source', ''), 'ms': d.get('ms', 0)} for d in top],
    }


def push_morning_report(db, report: dict):
    """One push notification pointing at the News tab — same registered-device list as push_alerts,
    never fatal (a delivery failure must not stop the run or lose the report already written)."""
    try:
        from firebase_admin import messaging
        tokens = [sn.id for sn in db.collection('news_devices').stream()]
    except Exception as e:  # noqa: BLE001
        print(f'  ! morning report push: devices unreadable ({e})', file=sys.stderr)
        return
    if not tokens:
        return
    parts = [f"{p} {report['products'][p]['net']:+d}" for p in PRODUCTS
              if report['products'].get(p, {}).get('n')]
    body = ('Overnight: ' + ', '.join(parts)) if parts else 'No qualifying overnight events.'
    title = '☀️ Morning Report — CTE'
    msg = messaging.MulticastMessage(
        tokens=tokens,
        notification=messaging.Notification(title=title, body=body[:180]),
        webpush=messaging.WebpushConfig(
            notification=messaging.WebpushNotification(title=title, body=body[:180], icon='/icon-192.png', tag='morning_report'),
            fcm_options=messaging.WebpushFCMOptions(link='https://commodity-trading-engine.web.app/#news'),
        ),
    )
    try:
        messaging.send_each_for_multicast(msg)
    except Exception as e:  # noqa: BLE001
        print(f'  ! morning report push failed ({e})', file=sys.stderr)


def maybe_send_morning_report(db):
    """Generates + writes the daily VN-07:00 report at most once per calendar date. Called on EVERY
    run (both the 'nothing new' early-return path and the normal path in main()) — cheap to check,
    and it must not depend on there being fresh headlines this particular run."""
    vn = _vn_now()
    if vn.hour != MORNING_REPORT_HOUR:
        return
    date_str = vn.strftime('%Y-%m-%d')
    ref = db.collection('news_meta').document('morning_report')
    try:
        existing = ref.get()
        if existing.exists and (existing.to_dict() or {}).get('dateVN') == date_str:
            return   # already generated today — every other run this hour is a no-op
    except Exception as e:  # noqa: BLE001
        print(f'  ! morning report: could not check existing ({e})', file=sys.stderr)
        return
    report = build_morning_report(db)
    if not report:
        return
    try:
        ref.set(report)
        db.collection('morning_reports').document(date_str).set(report)   # dated history copy
    except Exception as e:  # noqa: BLE001
        print(f'  ! morning report: write failed ({e})', file=sys.stderr)
        return
    print(f"  morning report generated for {date_str}")
    push_morning_report(db, report)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='do not write to Firestore')
    ap.add_argument('--mock', action='store_true', help='fake classifier, no Claude call')
    ap.add_argument('--out', help='also dump classified docs to this JSON file')
    args = ap.parse_args()

    model = os.environ.get('CLAUDE_MODEL', 'claude-sonnet-5')
    max_new = int(os.environ.get('NEWS_MAX_NEW', '80'))
    now_ms = int(time.time() * 1000)

    print('collecting…')
    stories = collect()
    print(f'  {len(stories)} unique stories in the last 3 days')

    verify_freshness(stories)

    cache = load_cache()
    seen_cache, events = cache['seen'], cache['events']
    db = None
    ids = list(stories)
    muted: set[str] = set()
    if not args.dry_run:
        db = firestore_client()
        _, muted = load_feedback(db)
        if muted:
            write_muted(db, muted)
            before = len(ids)
            ids = [i for i in ids if stories[i]['source'].strip().lower() not in muted]
            print(f'  {before - len(ids)} stories dropped from muted sources')
        cached = [i for i in ids if i in seen_cache]
        unknown = [i for i in ids if i not in seen_cache]
        seen = existing_ids(db, unknown) if unknown else set()   # only ask Firestore about ids the cache has never met
        for i in seen:
            seen_cache[i] = now_ms
        ids = [i for i in unknown if i not in seen]
        print(f'  {len(cached)} known from cache, {len(seen)} found in Firestore, {len(ids)} new')

    new_stories = sorted((stories[i] for i in ids), key=lambda s: -s['ms'])[:max_new]
    if not new_stories:
        if not args.dry_run:
            pruned = prune_old(db)
            save_cache(cache)
            sync_prices(db)
            maybe_send_morning_report(db)
            print(f'  pruned {pruned} events older than {RETENTION_DAYS} days')
        print('nothing new. done.')
        return

    hint = known_events_hint(events)
    if hint:
        print(f'  dedup context: {hint.count(chr(10)) - 1} known events from the last {DEDUP_HOURS}h')
    print(f'classifying {len(new_stories)} stories with {"MOCK" if args.mock else model}…')
    docs, processed = classify_all(new_stories, args.mock, model)
    failed = len(new_stories) - len(processed)
    print(f'  {len(docs)} kept, {len(processed) - len(docs)} skipped as irrelevant/routine'
          + (f', {failed} NOT processed (will retry next run)' if failed else ''))

    heads, updates = cluster(docs, events)
    print(f'  {len(heads)} events after clustering ({len(docs) - len(heads)} reports folded in, '
          f'{len(updates)} earlier events gained coverage)')

    if args.out:
        with open(args.out, 'w', encoding='utf-8') as f:
            json.dump(heads, f, ensure_ascii=False, indent=1)
        print(f'  dumped to {args.out}')

    if args.dry_run:
        for d in sorted(heads, key=lambda d: -d['ms'])[:15]:
            flag = ' BREAKING' if d.get('breaking') else ''
            print(f'  {d["category"]:8s} {d["product"]:7s} {d["direction"]:7s} imp {d["impact"]:3d} '
                  f'conf {d["confidence"]:3d} trend {d["trend"]:2d}{flag}  {d["headline"][:60]}')
        print('dry run — nothing written.')
        return

    write_docs(db, heads, updates)
    push_alerts(db, heads)
    pruned = prune_old(db)
    sync_prices(db)
    maybe_send_morning_report(db)
    # Everything the model actually answered for — kept, folded or skipped — is "seen": never pay
    # for it twice. Stories from a failed batch are left out so they come back next run.
    for s in new_stories:
        if s['id'] in processed:
            seen_cache[s['id']] = now_ms
    save_cache(cache)
    print(f'wrote {len(heads)} events (+{len(updates)} trend updates), pruned {pruned} old. done.')


if __name__ == '__main__':
    main()
