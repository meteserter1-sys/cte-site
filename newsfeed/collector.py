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
  4. WRITE     Firestore collection `news`, one document per story. The CTE page listens
               to that collection with onSnapshot and does the trailing-24h arithmetic itself.

Environment variables
  FIREBASE_SERVICE_ACCOUNT  JSON text of a Firebase service-account key   (required unless --dry-run)
  ANTHROPIC_API_KEY         Claude API key                                  (required unless --mock)
  CLAUDE_MODEL              default "claude-sonnet-4-5"
  NEWS_MAX_NEW              cap on new stories classified per run (default 80)

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

# Google News RSS search queries. Each entry: (query, hl, gl, ceid). Multilingual on purpose —
# the classifier reads any language and always answers in English.
GOOGLE_NEWS_QUERIES = [
    # English
    ('soybean meal price OR soymeal OR "soybean meal"',        'en-US', 'US', 'US:en'),
    ('soybean crush OR "soybean crop" OR "soy harvest"',      'en-US', 'US', 'US:en'),
    ('corn futures OR "corn crop" OR "corn exports"',         'en-US', 'US', 'US:en'),
    ('wheat futures OR "wheat crop" OR "wheat exports"',      'en-US', 'US', 'US:en'),
    ('dry bulk freight OR panamax OR supramax OR "Baltic Dry"', 'en-US', 'US', 'US:en'),
    ('Vietnam feed imports OR "Vietnam soybean meal" OR "Vietnam corn imports"', 'en-US', 'US', 'US:en'),
    ('Malaysia OR Indonesia "soybean meal" OR "corn imports" feed', 'en-US', 'US', 'US:en'),
    ('Argentina soybean OR Brazil soybean exports OR "farelo de soja"', 'en-US', 'US', 'US:en'),
    # Portuguese (Brazil)
    ('farelo de soja OR "exportação de soja" OR "safra de milho"', 'pt-BR', 'BR', 'BR:pt-419'),
    # Spanish (Argentina)
    ('harina de soja OR "cosecha de soja" OR "exportaciones de maíz"', 'es-419', 'AR', 'AR:es-419'),
    # Vietnamese
    ('khô đậu tương OR "bã đậu nành" OR "nhập khẩu ngô"',    'vi', 'VN', 'VN:vi'),
    # Indonesian
    ('bungkil kedelai OR "impor jagung" OR "harga jagung"',   'id', 'ID', 'ID:id'),
    # Chinese (demand side)
    ('豆粕 OR 大豆进口 OR 玉米进口',                            'zh-CN', 'CN', 'CN:zh-Hans'),
]

# Hand-picked publishers (the "sites" feed). Failures are tolerated — a dead feed is skipped.
SITE_FEEDS = [
    ('Brownfield Ag News',   'https://www.brownfieldagnews.com/feed/'),
    ('Farm Progress',        'https://www.farmprogress.com/rss.xml'),
    ('World-Grain',          'https://www.world-grain.com/rss/topic/2-news'),
    ('Notícias Agrícolas',   'https://www.noticiasagricolas.com.br/rss/soja.xml'),
    ('Hellenic Shipping',    'https://www.hellenicshippingnews.com/category/shipping-news/dry-bulk-market/feed/'),
]

PRODUCTS = ('SBM', 'CORN', 'WHEAT', 'FREIGHT', 'MACRO')
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
            stories[sid]['sourceCount'] += 1
            stories[sid]['ms'] = min(stories[sid]['ms'], entry_time_ms(entry))  # first publication wins
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
    return {k: v for k, v in stories.items() if v['ms'] >= cutoff}


# ---------------------------------------------------------------------------
# 2. Classification
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the news enricher for a commodity trading desk that IMPORTS soybean meal (SBM),
corn and wheat by sea into Vietnam, Malaysia and Indonesia, and charters dry-bulk vessels
(Panamax / Supramax, combination cargoes) from South America and the US Gulf.

For every headline you receive, answer with ONE JSON object per headline inside a JSON array,
same order as the input, nothing else. Fields:

  "i"          : the input index (integer)
  "product"    : one of "SBM","CORN","WHEAT","FREIGHT","MACRO","SKIP"
                 SBM = soybeans / soybean meal / crush / soy oil complex
                 FREIGHT = dry bulk rates, FFA, bunker fuel, canals, port congestion, vessel supply
                 MACRO = FX (USD/VND/MYR/IDR/BRL/ARS), interest rates, tariffs affecting the complex broadly
                 SKIP = not relevant to these markets (local retail prices, unrelated crops, sport, etc.)
  "direction"  : "BULLISH" | "BEARISH" | "NEUTRAL"
                 THIS IS A PRICE ARGUMENT, NOT SENTIMENT. BULLISH means the story argues the
                 CFR cost of that product UP (tighter supply, stronger demand, higher freight).
                 A record Brazilian crop is good news and BEARISH. An export ban is bad news and BULLISH.
  "impact"     : 0-100 integer, how much this could move the product's price. Routine daily
                 price wraps and local anecdotes are 15-35. Weather/crop shocks, policy changes,
                 big tenders, port disruptions are 55-90. Reserve 90+ for genuinely market-defining events.
  "confidence" : 0-100 integer, how sure you are of direction AND impact given only the headline.
  "country"    : ISO-3166 alpha-2 of the country the story is ABOUT ("" if global).
  "eventType"  : one of "WEATHER","SUPPLY DEMAND","TRADE FLOW","POLICY","GEOPOLITICS","LOGISTICS","PRICE","DISEASE","ENERGY","FX","OTHER"
  "summary"    : ONE English sentence (max 28 words) stating what happened AND why it matters
                 for the importer's cost, whatever language the headline is in.

Use round numbers (multiples of 5) for impact and confidence. Be consistent: the same kind of
story must get the same kind of score every time."""


def classify_with_claude(batch: list[dict], model: str) -> list[dict]:
    import anthropic  # local import so --mock works without the package configured

    client = anthropic.Anthropic()
    lines = []
    for i, s in enumerate(batch):
        snippet = f' | {s["snippet"][:200]}' if s.get('snippet') else ''
        lines.append(f'{i}. [{s["source"]}] {s["headline"]}{snippet}')
    user = 'Headlines:\n' + '\n'.join(lines) + '\n\nReturn the JSON array now.'

    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        temperature=0,
        system=SYSTEM_PROMPT,
        messages=[{'role': 'user', 'content': user}],
    )
    text = ''.join(b.text for b in resp.content if getattr(b, 'type', '') == 'text')
    m = re.search(r'\[.*\]', text, re.S)
    if not m:
        raise ValueError('no JSON array in model response: ' + text[:200])
    return json.loads(m.group(0))


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
    return {
        **{k: story[k] for k in ('id', 'feed', 'headline', 'url', 'source', 'sourceCount', 'ms')},
        'product': prod,
        'direction': direction,
        'impact': clamp(ans.get('impact')),
        'confidence': clamp(ans.get('confidence')),
        'country': str(ans.get('country', '') or '')[:2].upper(),
        'eventType': et,
        'summary': str(ans.get('summary', '') or '')[:300],
        'classifiedAt': int(time.time() * 1000),
    }


def classify_all(stories: list[dict], mock: bool, model: str, batch_size: int = 12) -> list[dict]:
    results: list[dict] = []
    for start in range(0, len(stories), batch_size):
        batch = stories[start:start + batch_size]
        try:
            answers = classify_mock(batch) if mock else classify_with_claude(batch, model)
        except Exception as e:  # noqa: BLE001
            print(f'  ! classify batch failed: {e}', file=sys.stderr)
            continue
        by_i = {int(a.get('i', -1)): a for a in answers if isinstance(a, dict)}
        for i, s in enumerate(batch):
            a = by_i.get(i)
            if not a:
                continue
            doc = sanitise(a, s)
            if doc:
                results.append(doc)
        print(f'  classified {min(start + batch_size, len(stories))}/{len(stories)}')
    return results


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
CACHE_DAYS = 7


def load_seen_cache() -> dict[str, int]:
    try:
        with open(CACHE_PATH, encoding='utf-8') as f:
            data = json.load(f)
        cutoff = int(time.time() * 1000) - CACHE_DAYS * 24 * 3600 * 1000
        return {k: v for k, v in data.items() if isinstance(v, int) and v >= cutoff}
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        print(f'  ! cache unreadable, starting fresh ({e})', file=sys.stderr)
        return {}


def save_seen_cache(cache: dict[str, int]):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, 'w', encoding='utf-8') as f:
        json.dump(cache, f)


def existing_ids(db, ids: list[str]) -> set[str]:
    found: set[str] = set()
    col = db.collection('news')
    for start in range(0, len(ids), 300):
        refs = [col.document(i) for i in ids[start:start + 300]]
        for snap in db.get_all(refs):
            if snap.exists:
                found.add(snap.id)
    return found


def write_docs(db, docs: list[dict]):
    from firebase_admin import firestore
    col = db.collection('news')
    for start in range(0, len(docs), 400):
        batch = db.batch()
        for d in docs[start:start + 400]:
            batch.set(col.document(d['id']), d)
        batch.commit()
    # heartbeat so the page can show "last update"
    db.collection('news_meta').document('status').set({
        'lastRunMs': int(time.time() * 1000),
        'lastRunIso': datetime.now(timezone.utc).isoformat(),
        'written': len(docs),
    })


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='do not write to Firestore')
    ap.add_argument('--mock', action='store_true', help='fake classifier, no Claude call')
    ap.add_argument('--out', help='also dump classified docs to this JSON file')
    args = ap.parse_args()

    model = os.environ.get('CLAUDE_MODEL', 'claude-sonnet-4-5')
    max_new = int(os.environ.get('NEWS_MAX_NEW', '80'))

    print('collecting…')
    stories = collect()
    print(f'  {len(stories)} unique stories in the last 3 days')

    db = None
    ids = list(stories)
    cache = load_seen_cache()
    if not args.dry_run:
        db = firestore_client()
        cached = [i for i in ids if i in cache]
        unknown = [i for i in ids if i not in cache]
        seen = existing_ids(db, unknown) if unknown else set()   # only ask Firestore about ids the cache has never met
        now_ms = int(time.time() * 1000)
        for i in seen:
            cache[i] = now_ms
        ids = [i for i in unknown if i not in seen]
        print(f'  {len(cached)} known from cache, {len(seen)} found in Firestore, {len(ids)} new')

    new_stories = sorted((stories[i] for i in ids), key=lambda s: -s['ms'])[:max_new]
    if not new_stories:
        if not args.dry_run:
            save_seen_cache(cache)
        print('nothing new. done.')
        return

    print(f'classifying {len(new_stories)} stories with {"MOCK" if args.mock else model}…')
    docs = classify_all(new_stories, args.mock, model)
    print(f'  {len(docs)} kept ({len(new_stories) - len(docs)} skipped as irrelevant/failed)')

    if args.out:
        with open(args.out, 'w', encoding='utf-8') as f:
            json.dump(docs, f, ensure_ascii=False, indent=1)
        print(f'  dumped to {args.out}')

    if args.dry_run:
        for d in docs[:15]:
            print(f'  {d["product"]:7s} {d["direction"]:7s} imp {d["impact"]:3d} conf {d["confidence"]:3d}  {d["headline"][:70]}')
        print('dry run — nothing written.')
        return

    write_docs(db, docs)
    # Everything classified this run — kept or skipped as irrelevant — is "seen": never pay for it twice.
    now_ms = int(time.time() * 1000)
    for s in new_stories:
        cache[s['id']] = now_ms
    save_seen_cache(cache)
    print(f'wrote {len(docs)} docs to Firestore. done.')


if __name__ == '__main__':
    main()
