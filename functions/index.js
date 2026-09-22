// 2026-09-18, Mete: backs the "Trader News" feature in index.html. A signed-in user pastes a
// trader's WhatsApp text and/or attaches a screenshot/PDF; this function sends it to Claude,
// holding the Anthropic API key server-side (it never reaches the browser), and returns a
// classification draft. It does NOT write to Firestore — the page shows the draft, lets Mete
// edit it, and only the "Add" click in index.html writes the doc (client-side, gated by the
// narrow `create` rule in firestore.rules). No dedup here on purpose: unlike collector.py's
// automatic pipeline, Trader News is manual by design and the desk decided not to bother
// checking it against already-known events for this lane.
const { onCall, HttpsError } = require('firebase-functions/v2/https');
const { defineSecret } = require('firebase-functions/params');
const logger = require('firebase-functions/logger');

const ANTHROPIC_API_KEY = defineSecret('ANTHROPIC_API_KEY');

// Kept in sync by hand with collector.py's PRODUCTS / CATEGORIES / EVENT_TYPES and with
// index.html's TRADER_PRODUCTS / TRADER_CATEGORIES. If one changes, change all three.
// 2026-09-22, Mete: FREIGHT retired here too — the desk stopped tracking freight/shipping-cost news
// entirely, automatic AND manual, so a Trader News item can no longer be tagged FREIGHT either.
const PRODUCTS = ['WHEAT', 'CORN', 'SBM'];
const CATEGORIES = ['POLITICS', 'AGRI', 'REPORTS'];
const EVENT_TYPES = [
  'WEATHER', 'SUPPLY DEMAND', 'TRADE FLOW', 'POLICY', 'GEOPOLITICS',
  'LOGISTICS', 'PRICE', 'DISEASE', 'ENERGY', 'FX', 'OTHER',
];

// Same desk framing as collector.py's SYSTEM_PROMPT, trimmed to a single manual item instead of
// a batch, and with a "headline" the model has to invent (a pasted WhatsApp message or a
// screenshot of one is not already a wire headline the way an RSS item is).
const SYSTEM_PROMPT = `You are the news enricher for a commodity trading desk that IMPORTS soybean meal (SBM),
corn and wheat by sea into South-East Asia and charters dry-bulk vessels (Panamax / Supramax,
combination cargoes) from South America and the US Gulf.

You will be given ONE item a trader sent the desk directly (pasted text, and/or a screenshot or
PDF of a chat/report). This is NOT a wire headline — it is often a longer market note, a chart
commentary, or a chat message. Read it (including any attached image/PDF) and call
classify_manual_news with:

  "headline"   : a short (max 100 chars) trading-desk-style headline summarizing the single most
                 important fact or argument in the message, in English, regardless of the
                 message's original language.
  "product"    : one of "SBM","CORN","WHEAT","SKIP"
                 SBM = soybeans / soybean meal / crush / soy oil complex / feed demand (hog, poultry herds)
                 2026-09-22, Mete: FREIGHT retired — dry bulk rates, FFA, bunker fuel, canals, port
                 congestion, vessel supply, and Red Sea / Suez / Bab-el-Mandeb disruption/rerouting
                 (including Houthi attacks) are now SKIP, full stop. The desk stopped tracking
                 freight/shipping-cost news entirely, manual entries included.
                 SKIP = not about SBM/CORN/WHEAT specifically (including former-FREIGHT topics above),
                        or pure chit-chat with no market content, general macro with no grain-supply
                        angle (Brent, Fed rate, DXY, VIX, bond yields), or a country that does not
                        shape world grain supply or demand. Countries that DO count: USA, Brazil, Argentina,
                        Russia, Ukraine, Kazakhstan, EU (France, Germany, Romania, Poland), Australia,
                        Canada, India, China, Egypt, Turkey, Pakistan and the Black Sea region. Still return your
                        best-guess fields even when product is SKIP — the desk may override product
                        by hand and keep the rest.
  "direction"  : "BULLISH" | "BEARISH" | "NEUTRAL" — a PRICE ARGUMENT, not sentiment. BULLISH means
                 the message argues the CFR cost of that product UP (tighter supply, stronger
                 demand, higher freight). A record crop / rising yield / rising crush is BEARISH
                 (more supply); a falling crop / yield cut / lower production / weak crush print is
                 BULLISH (less supply). An export ban is BULLISH. A market technical-levels note
                 with no fresh fundamental fact should reflect whatever bias the trader's own note
                 argues for (e.g. "still finds resistance at X, room to run" implied bullish tilt).
  "impact"     : 0-100 integer, how much this could move the product's WORLD price. Routine
                 updates / technical-level notes are 15-35. Weather/crop shocks, policy changes,
                 big tenders, port disruptions are 55-90. Reserve 90+ for market-defining events.
  "confidence" : 0-100 integer, how sure you are of direction AND impact given only this message.
  "country"    : ISO-3166 alpha-2 of the country the message is mainly about ("" if global/none).
  "category"   : "POLITICS" (policy, tariffs, export bans/duties, war, conflict, attacks, sanctions),
                 "AGRI" (crop, harvest, weather, yield, production, planting, ratings, ending
                 stocks, export numbers, feed demand), or "REPORTS" (citing a scheduled/official
                 release — USDA, NOAA, CONAB, Bolsa de Cereales, IKAR, SovEcon, IGC, FAO, ministry data).
  "breaking"   : true ONLY if this reports a discrete NEW incident/decision/data point that just
                 happened. false for analysis, recaps, technical commentary, or restating an
                 already-ongoing situation, even with dramatic wording.
  "eventType"  : one of "WEATHER","SUPPLY DEMAND","TRADE FLOW","POLICY","GEOPOLITICS","LOGISTICS","PRICE","DISEASE","ENERGY","FX","OTHER"
  "summary"    : ONE English sentence (max 28 words) stating what the message says AND why it
                 matters for the importer's cost.

Use round numbers (multiples of 5) for impact and confidence.`;

const CLASSIFY_TOOL = {
  name: 'classify_manual_news',
  description: "Return the desk's classification for this one manually-submitted trader message.",
  input_schema: {
    type: 'object',
    properties: {
      headline: { type: 'string' },
      product: { type: 'string', enum: [...PRODUCTS, 'SKIP'] },
      direction: { type: 'string', enum: ['BULLISH', 'BEARISH', 'NEUTRAL'] },
      impact: { type: 'integer' },
      confidence: { type: 'integer' },
      country: { type: 'string', description: 'ISO-3166 alpha-2, or "" if global' },
      category: { type: 'string', enum: CATEGORIES },
      breaking: { type: 'boolean' },
      eventType: { type: 'string', enum: EVENT_TYPES },
      summary: { type: 'string' },
    },
    required: ['headline', 'product', 'direction', 'impact', 'confidence', 'country', 'category', 'breaking', 'eventType', 'summary'],
  },
};

// Mirrors collector.py's sanitise(): never trust the model's enum/number output as-is, even
// though tool_choice forces a schema-shaped reply — a bad enum value or an out-of-range number
// should fall back to something safe rather than reach the browser (and, from there, Firestore).
function sanitizeDraft(ans) {
  const clamp = (v) => {
    const n = Math.round(Number(v));
    return Number.isFinite(n) ? Math.max(0, Math.min(100, n)) : 0;
  };
  let product = String(ans.product || 'SKIP').toUpperCase();
  if (product !== 'SKIP' && !PRODUCTS.includes(product)) product = 'SKIP';
  let direction = String(ans.direction || 'NEUTRAL').toUpperCase();
  if (!['BULLISH', 'BEARISH', 'NEUTRAL'].includes(direction)) direction = 'NEUTRAL';
  let category = String(ans.category || '').toUpperCase();
  if (!CATEGORIES.includes(category)) category = 'AGRI';
  let eventType = String(ans.eventType || 'OTHER').toUpperCase();
  if (!EVENT_TYPES.includes(eventType)) eventType = 'OTHER';
  const breaking = ans.breaking === true || String(ans.breaking).toLowerCase() === 'true';
  const headline = String(ans.headline || '').trim().slice(0, 180) || '(untitled)';
  return {
    headline,
    product,
    direction,
    impact: clamp(ans.impact),
    confidence: clamp(ans.confidence),
    country: String(ans.country || '').trim().slice(0, 2).toUpperCase(),
    category,
    breaking,
    eventType,
    summary: String(ans.summary || '').trim().slice(0, 300),
  };
}

// Base64 length bound roughly matching index.html's 5MB (TRADER_MAX_FILE_BYTES) client-side
// file-size cap — base64 runs ~4/3 the size of the original bytes.
const MAX_B64_CHARS = 7 * 1024 * 1024;

exports.classifyTraderNews = onCall({ secrets: [ANTHROPIC_API_KEY], timeoutSeconds: 90, memory: '256MiB' }, async (request) => {
  if (!request.auth) {
    throw new HttpsError('unauthenticated', 'Sign in required.');
  }
  const provider = request.auth.token && request.auth.token.firebase && request.auth.token.firebase.sign_in_provider;
  if (provider === 'anonymous') {
    // Matches firestore.rules' isAuthed(): anonymous sessions don't count as signed in here.
    throw new HttpsError('permission-denied', 'Anonymous sign-in is not allowed for this action.');
  }

  const data = request.data || {};
  const text = typeof data.text === 'string' ? data.text.trim() : '';
  const source = typeof data.source === 'string' ? data.source.trim() : '';
  const note = typeof data.note === 'string' ? data.note.trim() : '';
  const imageBase64 = typeof data.imageBase64 === 'string' ? data.imageBase64 : '';
  const imageMediaType = typeof data.imageMediaType === 'string' && data.imageMediaType ? data.imageMediaType : 'image/png';
  const pdfBase64 = typeof data.pdfBase64 === 'string' ? data.pdfBase64 : '';

  if (!text && !imageBase64 && !pdfBase64) {
    throw new HttpsError('invalid-argument', 'Provide text, an image, or a PDF.');
  }
  if (imageBase64.length > MAX_B64_CHARS || pdfBase64.length > MAX_B64_CHARS) {
    throw new HttpsError('invalid-argument', 'Attachment too large.');
  }

  const content = [];
  if (imageBase64) {
    content.push({ type: 'image', source: { type: 'base64', media_type: imageMediaType, data: imageBase64 } });
  }
  if (pdfBase64) {
    content.push({ type: 'document', source: { type: 'base64', media_type: 'application/pdf', data: pdfBase64 } });
  }
  let instructions = '';
  if (text) instructions += `Message text:\n${text}\n\n`;
  if (source) instructions += `Source / sender (from Mete, not part of the message itself): ${source}\n\n`;
  if (note) instructions += `Desk note / context (from Mete, not part of the message itself):\n${note}\n\n`;
  if (!text && (imageBase64 || pdfBase64)) {
    instructions += 'The message text was not pasted separately — read it from the attached image/PDF (e.g. a WhatsApp screenshot).\n\n';
  }
  instructions += 'Classify this by calling classify_manual_news.';
  content.push({ type: 'text', text: instructions });

  const model = process.env.CLAUDE_MODEL || 'claude-sonnet-5';

  let resp;
  try {
    resp = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-api-key': ANTHROPIC_API_KEY.value(),
        'anthropic-version': '2023-06-01',
      },
      body: JSON.stringify({
        model,
        max_tokens: 1024,
        system: SYSTEM_PROMPT,
        tools: [CLASSIFY_TOOL],
        tool_choice: { type: 'tool', name: 'classify_manual_news' },
        messages: [{ role: 'user', content }],
      }),
    });
  } catch (e) {
    logger.error('classifyTraderNews: fetch to Anthropic failed', e);
    throw new HttpsError('unavailable', 'Could not reach the classifier. Try again.');
  }

  if (!resp.ok) {
    const bodyText = await resp.text().catch(() => '');
    logger.error('classifyTraderNews: Anthropic API error', resp.status, bodyText.slice(0, 500));
    throw new HttpsError('internal', `Classifier error (${resp.status}).`);
  }

  const json = await resp.json();
  const block = (json.content || []).find((b) => b.type === 'tool_use' && b.name === 'classify_manual_news');
  if (!block || !block.input) {
    logger.error('classifyTraderNews: model did not call the tool', JSON.stringify(json).slice(0, 500));
    throw new HttpsError('internal', 'Classifier did not return a structured result.');
  }

  return sanitizeDraft(block.input);
});
