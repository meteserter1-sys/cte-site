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
  NEWS_RETENTION_DAYS       delete events older than this (default 10)
  NEWS_ALERT_IMPACT         push alert threshold (default 75; BREAKING always alerts)

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

# ── Sources (31, agreed with the desk on 2026-09-11) ────────────────────────
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
    # Broad on purpose: Houthi/Bab-el-Mandeb/Mocha are specific enough on their own that an AND
    # with "vessel/attack" words was dropping real coverage ("Why Yemen's Mocha port matters for
    # global trade" has neither) — 2026-09-11, after finding Houthi coverage missing entirely.
    ('Houthi OR "Bab-el-Mandeb" OR "Bab al-Mandeb" OR "Mocha port" OR "Red Sea shipping" OR "Red Sea security" OR "Suez Canal"',
                                                                                         'en-US', 'US', 'US:en'),
    ('пшеница экспорт OR урожай зерна OR "зерновой коридор" OR ИКАР',                   'ru', 'RU', 'RU:ru'),
    ('соя OR "соевый шрот" OR кукуруза экспорт OR порт Новороссийск зерно',             'ru', 'RU', 'RU:ru'),
    ('експорт зерна OR пшениця OR кукурудза OR "зерновий коридор" OR порт Одеса',      'uk', 'UA', 'UA:uk'),
    ('site:interfax.com OR site:interfax.com.ua grain OR wheat OR corn',                'en-US', 'US', 'US:en'),
    ('site:agroportal.ua',                                                              'uk', 'UA', 'UA:uk'),
    ('site:bfm.ru зерно OR пшеница OR экспорт',                                         'ru', 'RU', 'RU:ru'),
    # ── MACRO: only what reaches the grain complex ──
    ('"dry bulk" OR panamax OR supramax OR "Baltic Dry Index" OR "freight rates"',      'en-US', 'US', 'US:en'),
    ('"bunker fuel" price shipping freight',                                            'en-US', 'US', 'US:en'),
    ('Brent crude OR "Fed rate" OR "dollar index" OR VIX commodities',                  'en-US', 'US', 'US:en'),
]

# Direct publisher RSS (addresses taken from the desk's Inoreader) and X bridges. A dead feed is
# logged and skipped; the run never stops for one source.
SITE_FEEDS: list[tuple[str, str]] = [
    # WORLD
    ('War on the Rocks',              'https://warontherocks.com/feed/'),
    ('ISW (X)',                       'https://rss.xcancel.com/TheStudyofWar/rss'),
    # CROP / REPORTS
    ('USDA News',                     'https://www.usda.gov/rss/latest-releases.xml'),
    ('USDA NASS',                     'https://www.nass.usda.gov/rss/reports.xml'),
    ('World Grain',                   'https://www.world-grain.com/rss/articles'),
    ('IGC via World Grain',           'https://www.world-grain.com/rss/topic/1069-igc-international-grains-council'),
    ('Karen Braun (X)',               'https://rss.xcancel.com/kannbwx/rss'),
    ('Andrey Sizov (X)',              'https://rss.xcancel.com/sizov_andre/rss'),
    # FREIGHT — dedicated trade press; the Google News keyword queries above miss most of what
    # these actually publish (dry-bulk rates, canal/strait disruption, vessel supply). Added
    # 2026-09-11 after comparing against the desk's Inoreader: FreightWaves and Lloyd's List were
    # both completely absent. FreightWaves' RSS is a general firehose (trucking included, not
    # just ocean freight) — the classifier's own FREIGHT/SKIP rules do the filtering, same as
    # every other broad source here.
    ('FreightWaves',                  'https://www.freightwaves.com/news/feed'),
]

PRODUCTS = ('SBM', 'CORN', 'WHEAT', 'FREIGHT', 'MACRO')
# "Tight wire": stories the classifier scores below this impact are relevant but routine (daily
# wraps, local anecdotes). They are NOT stored — the desk chose a short list over a full one.
# Classification cost is unchanged; only Firestore and the page get quieter.
NEWS_MIN_STORE = int(os.environ.get('NEWS_MIN_STORE', '30'))
CATEGORIES = ('POLITICS', 'AGRI', 'REPORTS', 'MACRO')
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

    for q, hl, gl, ceid in GOOGLE_NEWS_QUERIES:
        fp = fetch_feed(gnews_url(q, hl, gl, ceid))
        if not fp:
            continue
        for e in fp.entries[:40]:
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
  "product"    : one of "SBM","CORN","WHEAT","FREIGHT","MACRO","SKIP"
                 SBM = soybeans / soybean meal / crush / soy oil complex / feed demand (hog, poultry herds)
                 FREIGHT = dry bulk rates, FFA, bunker fuel, canals, port congestion, vessel supply.
                           Red Sea / Suez Canal / Bab-el-Mandeb disruption (Houthi attacks, vessel
                           strikes, rerouting via the Cape of Good Hope) is FREIGHT — it raises transit
                           time and cost on Asia-bound trade lanes. Classify it as FREIGHT regardless of
                           country; do not SKIP it as "Yemen/Israel/Iran domestic news".
                 MACRO = only macro that reaches the grain complex: Brent and natural gas (fertiliser,
                         biofuel), Fed rate / dollar index (export competitiveness), BRL and ARS
                         (farmer selling), VIX only when it is a broad commodity risk-off
                 SKIP = everything else. SKIP ALSO when the story is:
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
                 SYMMETRY RULE: crop/yield/rating headlines are two-sided. A record or rising crop/yield/
                 rating is BEARISH (more supply) — and its mirror image, a FALLING crop rating, a yield cut,
                 a downgraded harvest estimate, or a lower-than-expected production number, is BULLISH (less
                 supply). Apply this consistently in both directions: do not let "bad news for farmers"
                 framing pull a shrinking crop toward BEARISH, and do not let "good news for farmers"
                 framing pull a growing crop toward BULLISH. The price argument runs on supply tightness,
                 not on whether the news sounds good or bad for the growing country or its farmers.
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
  "category"   : the desk's reading lane, one of
                 "POLITICS" = government policy, tariffs, export bans/duties, war, conflict, attacks, explosions, sanctions
                 "AGRI"     = crop, harvest, weather, yield, production, planting, good/excellent ratings, ending stocks, export numbers, feed demand
                 "REPORTS"  = a scheduled/official release or data from USDA, NOAA, CONAB, Bolsa de Cereales de Buenos Aires,
                              IKAR, SovEcon, IGC, FAO, ministry statistics — the source itself is an agency or its report
                 "MACRO"    = freight, Brent/energy, Fed rate, dollar index, VIX, FX
  "breaking"   : true ONLY for a fresh, market-defining development on these markets (port attack, sudden export ban,
                 major crop-estimate shock, big surprise tender). Routine updates are false. Expect fewer than 1 in 10.
  "eventType"  : one of "WEATHER","SUPPLY DEMAND","TRADE FLOW","POLICY","GEOPOLITICS","LOGISTICS","PRICE","DISEASE","ENERGY","FX","OTHER"
  "summary"    : ONE English sentence (max 28 words) stating what happened AND why it matters
                 for the importer's cost, whatever language the headline is in.

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
                    },
                    'required': ['i', 'product', 'direction', 'impact', 'confidence',
                                 'country', 'category', 'breaking', 'eventType', 'summary'],
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
        system=SYSTEM_PROMPT + FEEDBACK_HINT,
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
        prod = PRODUCTS[h % 4]
        direction = ('BULLISH', 'BEARISH', 'NEUTRAL')[(h >> 4) % 3]
        out.append({
            'i': i, 'product': prod, 'direction': direction,
            'impact': 30 + 5 * ((h >> 8) % 13), 'confidence': 40 + 5 * ((h >> 12) % 11),
            'country': ('US', 'BR', 'AR', 'VN', 'CN', '')[(h >> 16) % 6],
            'category': CATEGORIES[(h >> 18) % len(CATEGORIES)],
            'breaking': (h >> 22) % 10 == 0,
            'eventType': EVENT_TYPES[(h >> 20) % len(EVENT_TYPES)],
            'summary': f'[mock] {s["headline"][:80]}',
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
        cat = 'MACRO' if prod in ('FREIGHT', 'MACRO') else 'AGRI'
    breaking = ans.get('breaking')
    breaking = breaking is True or str(breaking).lower() == 'true'
    return {
        **{k: story[k] for k in ('id', 'feed', 'headline', 'url', 'source', 'sourceCount', 'ms')},
        'product': prod,
        'direction': direction,
        'impact': clamp(ans.get('impact')),
        'confidence': clamp(ans.get('confidence')),
        'country': str(ans.get('country', '') or '')[:2].upper(),
        'category': cat,
        'breaking': breaking,
        'eventType': et,
        'summary': str(ans.get('summary', '') or '')[:300],
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
# Country/region names (China, Russia, Brazil, ...) are deliberately NOT stopwords: they are
# often the one word that ties two differently-worded reports of the very same event together
# (e.g. two outlets both covering "China buys US soybeans" on the same day). The `same_event()`
# country gate below only fires when BOTH docs carry a tagged country, so it isn't a substitute
# for this signal on its own.


def event_tokens(doc: dict) -> list[str]:
    text = f"{doc.get('summary', '')} {doc.get('headline', '')}".lower()
    words = re.findall(r'[a-z0-9]{3,}', text)
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


# ── Retention: 10 days, per the desk. The History snapshot keeps each day's reading. ─────
RETENTION_DAYS = int(os.environ.get('NEWS_RETENTION_DAYS', '10'))


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
            print(f'  pruned {pruned} events older than {RETENTION_DAYS} days')
        print('nothing new. done.')
        return

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
    # Everything the model actually answered for — kept, folded or skipped — is "seen": never pay
    # for it twice. Stories from a failed batch are left out so they come back next run.
    for s in new_stories:
        if s['id'] in processed:
            seen_cache[s['id']] = now_ms
    save_cache(cache)
    print(f'wrote {len(heads)} events (+{len(updates)} trend updates), pruned {pruned} old. done.')


if __name__ == '__main__':
    main()
