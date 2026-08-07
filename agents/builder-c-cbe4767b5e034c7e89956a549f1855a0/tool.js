'use strict';
/*
 * RescuePulse - Disaster Intelligence & Rescue Prioritization Engine
 * A zero-dependency, single-file tool for emergency response teams.
 * Parses unstructured disaster reports (SMS, social posts, radio transcripts),
 * scores severity across multiple dimensions, and produces a prioritized
 * rescue plan with resource allocation suggestions.
 *
 * Usage:
 *   node tool.js --self-test              run all self checks
 *   node tool.js --demo                   generate demo report (stdout)
 *   node tool.js --demo --html out.html   write styled HTML report
 *   node tool.js --reports file.txt       analyze reports from a file
 *   node tool.js --reports -              analyze reports from stdin
 *   node tool.js --report "TEXT"          analyze a single report string
 *
 * AI for Good track - Agentathon
 */

const fs = require('fs');

const VERSION = '1.0.0';

/* ============================== utilities ============================== */

function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }
function hashStr(s) {
  let h = 2166136261 >>> 0;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619) >>> 0;
  }
  return h >>> 0;
}
function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/* ============================ report parsing ============================ */

// keyword -> category detection with weights
const CATEGORIES = {
  flood: {
    label: 'Flood', keywords: ['flood', 'flooding', 'water level', 'rising water', 'submerged', 'inundat', 'dam break', 'levee', 'rain', 'torrential'],
    base: 0.35, color: '#1e88e5'
  },
  earthquake: {
    label: 'Earthquake', keywords: ['earthquake', 'quake', 'aftershock', 'tremor', 'magnitude', 'seismic', 'epicenter'],
    base: 0.6, color: '#e53935'
  },
  fire: {
    label: 'Fire', keywords: ['fire', 'burning', 'wildfire', 'blaze', 'smoke', 'flame'],
    base: 0.55, color: '#fb8c00'
  },
  medical: {
    label: 'Medical', keywords: ['injur', 'wound', 'bleed', 'patient', 'hospital', 'medical', 'ambulance', 'casualt', 'critical', 'unconscious', 'broken', 'fracture', 'sick', 'disease', 'outbreak', 'fever'],
    base: 0.5, color: '#8e24aa'
  },
  trapped: {
    label: 'Trapped', keywords: ['trapped', 'stranded', 'stuck', 'under rubble', 'collapsed', 'pinned', 'shelter', 'rooftop', 'cut off', 'isolated'],
    base: 0.65, color: '#d81b60'
  },
  foodwater: {
    label: 'Food/Water', keywords: ['food', 'water', 'drinking water', 'hungry', 'thirst', 'supplies', 'ration', 'starving'],
    base: 0.3, color: '#00897b'
  },
  infrastructure: {
    label: 'Infrastructure', keywords: ['power', 'electric', 'electricity', 'bridge', 'road', 'highway', 'damage', 'destroyed', 'collapsed', 'blackout', 'outage', 'network', 'communication', 'phone line', 'cell tower'],
    base: 0.4, color: '#546e7a'
  },
  landslide: {
    label: 'Landslide', keywords: ['landslide', 'mudslide', 'rockfall', 'debris flow', 'avalanche'],
    base: 0.55, color: '#6d4c41'
  },
  storm: {
    label: 'Storm', keywords: ['storm', 'cyclone', 'typhoon', 'hurricane', 'tornado', 'wind', 'gale'],
    base: 0.5, color: '#3949ab'
  },
  tsunami: {
    label: 'Tsunami', keywords: ['tsunami', 'tidal wave', 'storm surge'],
    base: 0.7, color: '#00acc1'
  }
};

// casualty / people-count regexes
const RE_PEOPLE = /(\d+)\s*(?:people|persons|residents|families|villagers|civilians|trapped|stranded|injured|wounded|dead|killed|missing|affected|homeless|households|families)/i;
const RE_INJURED = /(\d+)\s*(?:injured|wounded|casualties)/i;
const RE_DEAD = /(\d+)\s*(?:dead|killed|deaths|fatalities)/i;
const RE_MISSING = /(\d+)\s*(?:missing|unaccounted)/i;
const RE_TRAPPED = /(\d+)\s*(?:people\s+|residents\s+|families\s+)?(?:trapped|stranded)/i;
const RE_LOCATION = /(?:in|at|near|around|north of|south of|east of|west of)\s+([A-Z][a-zA-Z .'-]{2,40})/;
const RE_COORDS = /(-?\d{1,3}(?:\.\d+)?)[,\s]+(-?\d{1,3}(?:\.\d+)?)/;

function extractCount(text, re) {
  const m = text.match(re);
  if (m) {
    const n = parseInt(m[1], 10);
    if (!isNaN(n) && n > 0 && n < 1000000) return n;
  }
  return 0;
}

function detectCategory(text) {
  const t = text.toLowerCase();
  // tier 1: disaster event types (trapped is a state, not an event)
  const tier1 = ['tsunami', 'landslide', 'storm', 'earthquake', 'fire', 'flood'];
  let best = null, bestScore = 0;
  for (const key of Object.keys(CATEGORIES)) {
    let score = 0;
    for (const kw of CATEGORIES[key].keywords) {
      if (t.indexOf(kw) !== -1) score += 1;
    }
    // boost tier-1 categories so "trapped" beats "injured/medical"
    if (tier1.indexOf(key) !== -1) score *= (key === 'tsunami' ? 2.0 : 1.5);
    if (score > bestScore) { bestScore = score; best = key; }
  }
  return best || 'general';
}

function parseReport(raw, meta) {
  const text = String(raw || '').trim();
  const t = text.toLowerCase();
  const loc = (text.match(RE_LOCATION) || [])[1] || (meta && meta.location) || 'Unknown';
  const coords = text.match(RE_COORDS);
  const lat = coords ? parseFloat(coords[1]) : null;
  const lng = coords ? parseFloat(coords[2]) : null;
  const cat = detectCategory(text);
  const people = extractCount(text, RE_PEOPLE);
  const injured = extractCount(text, RE_INJURED);
  const dead = extractCount(text, RE_DEAD);
  const missing = extractCount(text, RE_MISSING);
  const trapped = extractCount(text, RE_TRAPPED);
  const ts = (meta && meta.timestamp) || Date.now();
  return {
    id: meta && meta.id ? meta.id : 'r' + hashStr(text + ':' + ts).toString(36),
    text,
    location: loc,
    lat, lng,
    category: cat,
    categoryLabel: cat === 'general' ? 'General' : CATEGORIES[cat].label,
    people, injured, dead, missing, trapped,
    timestamp: ts,
    source: (meta && meta.source) || 'unknown',
    reliability: meta && meta.reliability ? clamp(meta.reliability, 0, 1) : 0.7,
    verified: !!(meta && meta.verified),
    raw: meta || {}
  };
}

/* ========================= severity scoring ========================== */

const SEVERITY_WEIGHTS = {
  lives: 0.34,       // direct threat to life (trapped, injured, dead)
  scale: 0.2,        // number of people affected
  hazard: 0.18,      // category inherent danger
  urgency: 0.14,     // time decay / recency
  secondary: 0.09,   // infrastructure / secondary risk
  credibility: 0.05  // source reliability & verification
};

function livesCurve(direct, affected) {
  const x = direct + affected * 0.6;
  if (x <= 0) return 0.05;
  if (x < 10) return 0.2 + (x / 10) * 0.3;
  if (x < 100) return 0.5 + ((x - 10) / 90) * 0.3;
  return 0.8 + Math.min(0.2, Math.log10(x / 100) / 5);
}

function scoreReport(r, now) {
  const n = now || Date.now();
  const ageH = Math.max(0, (n - r.timestamp) / 3600000);
  const catBase = r.category === 'general' ? 0.25 : CATEGORIES[r.category].base;

  // lives at risk: combination of trapped/injured/dead + people affected
  const direct = r.trapped * 1.6 + r.injured * 1.3 + r.dead * 1.8 + r.missing * 1.1;
  const affected = r.people;
  const lives = livesCurve(direct, affected);

  // scale: log-scaled population exposure
  const scale = livesCurve(affected, direct * 0.5);

  // hazard: category base + presence of trapped/medical keywords amplification
  let hazard = catBase;
  if (r.trapped > 0) hazard += 0.15;
  if (r.injured > 0) hazard += 0.1;
  if (r.dead > 0) hazard += 0.1;
  hazard = clamp(hazard, 0, 1);

  // urgency: fresher reports score higher (2h half-life)
  const urgency = Math.exp(-ageH / 2);

  // secondary risk: infrastructure damage implies follow-on risk
  const secondary = r.category === 'infrastructure' ? 0.5 :
    r.category === 'fire' ? 0.4 : (r.people > 50 ? 0.3 : 0.1);

  // credibility
  const credibility = r.verified ? Math.min(1, r.reliability + 0.2) : r.reliability;

  const components = { lives, scale, hazard, urgency, secondary, credibility };
  let total = 0;
  for (const k of Object.keys(SEVERITY_WEIGHTS)) {
    total += SEVERITY_WEIGHTS[k] * components[k];
  }
  total = clamp(total, 0, 1);

  const level = total >= 0.75 ? 'CRITICAL' : total >= 0.5 ? 'HIGH' : total >= 0.3 ? 'MEDIUM' : 'LOW';
  return { score: total, level, components, ageH, catBase };
}

/* ====================== prioritization & planning ======================= */

function prioritize(reports, opts) {
  const o = opts || {};
  const now = o.now || Date.now();
  const scored = reports.map((r) => {
    const s = scoreReport(r, now);
    return { report: r, ...s };
  });
  scored.sort((a, b) => b.score - a.score || a.report.timestamp - b.report.timestamp);
  // assign priorities 1..N and resource bundles
  scored.forEach((s, i) => {
    s.priority = i + 1;
    s.resources = allocateResources(s, i, scored.length);
  });
  return scored;
}

function allocateResources(s, rank, total) {
  const bundle = [];
  if (s.level === 'CRITICAL') {
    bundle.push('medical_team', 'rescue_team', 'helicopter', 'ambulance');
    if (s.report.category === 'fire') bundle.push('fire_engine', 'water_tanker');
    if (s.report.category === 'flood') bundle.push('boat', 'life_jackets');
  } else if (s.level === 'HIGH') {
    bundle.push('medical_team', 'rescue_team');
    if (s.report.category === 'flood') bundle.push('boat');
    if (s.report.category === 'foodwater') bundle.push('water_truck', 'food_packs');
  } else if (s.level === 'MEDIUM') {
    bundle.push('assessment_team');
    if (s.report.category === 'foodwater') bundle.push('food_packs');
    if (s.report.category === 'infrastructure') bundle.push('engineer_team');
  } else {
    bundle.push('assessment_team');
  }
  return bundle;
}

function timeAgo(ts, now) {
  const m = Math.max(0, Math.round((now - ts) / 60000));
  if (m < 1) return 'just now';
  if (m < 60) return m + ' min ago';
  const h = Math.floor(m / 60);
  if (h < 24) return h + 'h ' + (m % 60) + 'm ago';
  return Math.floor(h / 24) + ' days ago';
}

function formatReport(r, s, now) {
  const lines = [];
  lines.push('[' + s.priority + '] ' + s.level + '  ' + s.report.categoryLabel + ' @ ' + s.report.location);
  lines.push('    Score: ' + s.score.toFixed(3) + '  People: ' + (s.report.people || 0) +
    '  Injured: ' + (s.report.injured || 0) + '  Dead: ' + (s.report.dead || 0) +
    '  Trapped: ' + (s.report.trapped || 0));
  lines.push('    Age: ' + timeAgo(s.report.timestamp, now) + '  Source: ' + s.report.source +
    '  Rel: ' + (s.report.reliability * 100 | 0) + '%' + (s.report.verified ? ' [VERIFIED]' : ''));
  lines.push('    Resources: ' + s.resources.join(', '));
  lines.push('    "' + (s.report.text.length > 140 ? s.report.text.slice(0, 140) + '...' : s.report.text) + '"');
  return lines.join('\n');
}

function buildPlan(reports, opts) {
  const o = opts || {};
  const now = o.now || Date.now();
  const scored = prioritize(reports, { now });
  const critical = scored.filter((s) => s.level === 'CRITICAL').length;
  const high = scored.filter((s) => s.level === 'HIGH').length;
  const byCat = {};
  for (const s of scored) {
    byCat[s.report.categoryLabel] = (byCat[s.report.categoryLabel] || 0) + 1;
  }
  const text = [];
  text.push('==========================================================');
  text.push('  RESCUEPULSE - Disaster Response Plan');
  text.push('  Generated: ' + new Date(now).toISOString());
  text.push('  Reports analyzed: ' + scored.length);
  text.push('  Critical: ' + critical + ' | High: ' + high + ' | Other: ' + (scored.length - critical - high));
  text.push('  Categories: ' + Object.keys(byCat).map((k) => k + '=' + byCat[k]).join(', '));
  text.push('==========================================================');
  text.push('');
  for (const s of scored) {
    text.push(formatReport(s.report, s, now));
    text.push('');
  }
  const top = scored.slice(0, Math.min(3, scored.length));
  if (top.length) {
    text.push('--- Priority Actions ---');
    top.forEach((s, i) => {
      text.push((i + 1) + '. Dispatch ' + s.resources.join(' + ') + ' to ' + s.report.location +
        ' (' + s.report.categoryLabel + ', score ' + s.score.toFixed(2) + ')');
    });
  }
  return { text: text.join('\n'), scored, critical, high, byCat };
}

/* ============================ HTML report ============================= */

function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function colorForLevel(level) {
  return level === 'CRITICAL' ? '#c62828' : level === 'HIGH' ? '#ef6c00' : level === 'MEDIUM' ? '#f9a825' : '#2e7d32';
}

function htmlReport(plan, opts) {
  const o = opts || {};
  const now = o.now || Date.now();
  const rows = plan.scored.map((s) => {
    const r = s.report;
    return '<tr>' +
      '<td class="prio">' + s.priority + '</td>' +
      '<td><span class="level" style="background:' + colorForLevel(s.level) + '">' + s.level + '</span></td>' +
      '<td>' + escapeHtml(r.categoryLabel) + '</td>' +
      '<td>' + escapeHtml(r.location) + '</td>' +
      '<td>' + (r.people || 0) + '</td>' +
      '<td>' + (r.injured || 0) + '</td>' +
      '<td>' + (r.dead || 0) + '</td>' +
      '<td>' + (r.trapped || 0) + '</td>' +
      '<td>' + s.score.toFixed(3) + '</td>' +
      '<td>' + timeAgo(r.timestamp, now) + '</td>' +
      '<td>' + escapeHtml(s.resources.join(', ')) + '</td>' +
      '<td class="quote">' + escapeHtml(r.text.length > 90 ? r.text.slice(0, 90) + '…' : r.text) + '</td>' +
      '</tr>';
  }).join('\n');
  const bars = plan.scored.map((s) => {
    const pct = Math.round(s.score * 100);
    return '<div class="bar-row"><div class="bar-label">#' + s.priority + ' ' +
      escapeHtml(s.report.categoryLabel + ' @ ' + s.report.location) + '</div>' +
      '<div class="bar-track"><div class="bar-fill" style="width:' + pct + '%;background:' + colorForLevel(s.level) + '"></div></div>' +
      '<div class="bar-val">' + pct + '</div></div>';
  }).join('\n');
  return '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width,initial-scale=1">' +
    '<title>RescuePulse Report</title><style>' +
    'body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#0f172a;color:#e2e8f0;padding:24px}' +
    'h1{font-size:22px;margin:0 0 4px}.sub{color:#94a3b8;font-size:13px;margin-bottom:20px}' +
    'table{border-collapse:collapse;width:100%;font-size:12px;background:#1e293b;border-radius:8px;overflow:hidden}' +
    'th{background:#334155;text-align:left;padding:8px 6px;font-size:11px;text-transform:uppercase;letter-spacing:.5px}' +
    'td{padding:7px 6px;border-top:1px solid #334155;vertical-align:top}' +
    '.prio{font-weight:700;color:#93c5fd}.level{color:#fff;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:700}' +
    '.quote{color:#94a3b8;max-width:260px}.stats{display:flex;gap:16px;margin:16px 0;flex-wrap:wrap}' +
    '.stat{background:#1e293b;border-radius:8px;padding:12px 18px;min-width:110px}' +
    '.stat b{font-size:22px;display:block}.stat span{font-size:11px;color:#94a3b8}' +
    '.bars{margin-top:20px}.bar-row{display:flex;align-items:center;gap:10px;margin:6px 0}' +
    '.bar-label{width:260px;font-size:11px;text-align:right;color:#cbd5e1}' +
    '.bar-track{flex:1;background:#334155;border-radius:4px;height:16px;overflow:hidden}' +
    '.bar-fill{height:100%;border-radius:4px;transition:width .5s}' +
    '.bar-val{width:34px;font-size:11px;font-weight:700}footer{margin-top:24px;font-size:11px;color:#64748b}' +
    '</style></head><body>' +
    '<h1>🚨 RescuePulse — Disaster Response Plan</h1>' +
    '<div class="sub">Generated ' + new Date(now).toISOString() + ' · ' + plan.scored.length + ' reports · zero-dependency offline engine</div>' +
    '<div class="stats">' +
    '<div class="stat"><b style="color:#f87171">' + plan.critical + '</b><span>Critical</span></div>' +
    '<div class="stat"><b style="color:#fb923c">' + plan.high + '</b><span>High</span></div>' +
    '<div class="stat"><b style="color:#93c5fd">' + plan.scored.length + '</b><span>Total Reports</span></div>' +
    '</div>' +
    '<table><thead><tr><th>#</th><th>Level</th><th>Category</th><th>Location</th><th>People</th><th>Injured</th><th>Dead</th><th>Trapped</th><th>Score</th><th>Age</th><th>Resources</th><th>Report</th></tr></thead>' +
    '<tbody>' + rows + '</tbody></table>' +
    '<div class="bars">' + bars + '</div>' +
    '<footer>RescuePulse v' + VERSION + ' — offline, deterministic, zero third-party dependencies. All processing happens locally; suitable for low-bandwidth disaster zones.</footer>' +
    '</body></html>';
}

/* ============================ demo dataset ============================ */

const DEMO_REPORTS = [
  { source: 'sms', timestamp: Date.now() - 25 * 60000, reliability: 0.8, verified: true, text: 'Flooding severe in Riverside Village, 300 people trapped on rooftops, water rising fast, 12 injured. Need boats urgently.' },
  { source: 'social', timestamp: Date.now() - 2 * 3600000, reliability: 0.6, verified: false, text: 'Building collapsed near Market Street after earthquake, 45 people trapped under rubble, 8 injured, 3 dead confirmed.' },
  { source: 'radio', timestamp: Date.now() - 4 * 3600000, reliability: 0.9, verified: true, text: 'Wildfire spreading towards Hillcrest district, 150 residents evacuated, 20 injured with burns, smoke affecting visibility.' },
  { source: 'sms', timestamp: Date.now() - 30 * 60000, reliability: 0.7, verified: false, text: 'Bridge on Highway 5 washed out by flood, 60 vehicles stranded, 5 people injured, no power in nearby towns.' },
  { source: 'social', timestamp: Date.now() - 8 * 3600000, reliability: 0.5, verified: false, text: 'Hearing reports of water contamination in Lakeside, 200 families without clean drinking water, some sick with fever.' },
  { source: 'sms', timestamp: Date.now() - 3 * 3600000, reliability: 0.85, verified: true, text: 'School gymnasium shelter at North Hill is overcrowded, 400 people, running out of food and water supplies.' },
  { source: 'radio', timestamp: Date.now() - 6 * 3600000, reliability: 0.8, verified: true, text: 'Earthquake aftershock damaged power grid in East Zone, 3 hospitals on backup generators, 2 bridges closed.' },
  { source: 'social', timestamp: Date.now() - 12 * 3600000, reliability: 0.4, verified: false, text: 'Unconfirmed: possible gas leak in industrial park, some workers reporting dizziness. Awaiting official confirmation.' }
];

function demoData(now) {
  return DEMO_REPORTS.map((r, i) => parseReport(r.text, {
    id: 'demo' + (i + 1), timestamp: r.timestamp, source: r.source,
    reliability: r.reliability, verified: r.verified, location: r.location
  }));
}

/* ============================ CLI entry =============================== */

function run(argv) {
  const args = argv.slice(2);
  const opts = {};
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === '--self-test') opts.selfTest = true;
    else if (a === '--demo') opts.demo = true;
    else if (a === '--html') opts.html = args[++i];
    else if (a === '--report') opts.report = args[++i];
    else if (a === '--reports') opts.reports = args[++i];
    else if (a === '--now') opts.now = parseInt(args[++i], 10);
    else if (a === '--help' || a === '-h') opts.help = true;
  }
  if (opts.help) { printHelp(); return 0; }
  if (opts.selfTest) return selfTest() ? 0 : 1;
  if (opts.report) {
    const r = parseReport(opts.report, { source: 'manual', reliability: 0.7 });
    const plan = buildPlan([r], { now: opts.now || Date.now() });
    if (opts.html) { fs.writeFileSync(opts.html, htmlReport(plan, { now: opts.now || Date.now() })); console.log('HTML report written to ' + opts.html); return 0; }
    console.log(plan.text); return 0;
  }
  if (opts.reports) {
    let data = '';
    if (opts.reports === '-') data = fs.readFileSync(0, 'utf8');
    else data = fs.readFileSync(opts.reports, 'utf8');
    const reports = data.split(/\n{2,}|\r?\n(?=[A-Z0-9#])/).map((chunk) => chunk.trim()).filter((c) => c.length > 10)
      .map((chunk, i) => parseReport(chunk, { source: 'file:' + opts.reports, reliability: 0.6, id: 'f' + (i + 1) }));
    const plan = buildPlan(reports, { now: opts.now || Date.now() });
    if (opts.html) { fs.writeFileSync(opts.html, htmlReport(plan, { now: opts.now || Date.now() })); console.log('HTML report written to ' + opts.html); return 0; }
    console.log(plan.text); return 0;
  }
  // default: demo
  const now = opts.now || Date.now();
  const plan = buildPlan(demoData(now), { now });
  if (opts.html) { fs.writeFileSync(opts.html, htmlReport(plan, { now })); console.log('HTML report written to ' + opts.html); return 0; }
  console.log(plan.text);
  return 0;
}

function printHelp() {
  console.log([
    'RescuePulse v' + VERSION + ' — Disaster Intelligence & Rescue Prioritization',
    '',
    'Usage:',
    '  node tool.js --self-test              run self checks',
    '  node tool.js --demo                   generate demo response plan',
    '  node tool.js --demo --html out.html   write styled HTML report',
    '  node tool.js --report "TEXT"          analyze one report string',
    '  node tool.js --reports file.txt       analyze reports file',
    '  node tool.js --reports -              analyze reports from stdin',
    '',
    'Examples:',
    '  node tool.js --report "300 people trapped in flood near Riverside, 12 injured"',
    '  node tool.js --demo --html rescue-plan.html'
  ].join('\n'));
}

/* ============================ self test ============================= */

function selfTest() {
  const results = [];
  const check = (name, cond, extra) => results.push({ name, pass: !!cond, extra: extra || '' });

  // 1. parsing: category detection
  const f = parseReport('Flooding severe in Riverside Village, 300 people trapped on rooftops, 12 injured', { timestamp: Date.now(), source: 'sms', reliability: 0.8 });
  check('category detection (flood)', f.category === 'flood');
  check('people count extraction', f.people === 300);
  check('injured extraction', f.injured === 12);
  check('location extraction', f.location === 'Riverside Village');

  // 2. earthquake parsing
  const e = parseReport('Building collapsed near Market Street after earthquake, 45 people trapped, 3 dead', { timestamp: Date.now(), source: 'social', reliability: 0.6 });
  check('earthquake category', e.category === 'earthquake');
  check('trapped extraction', e.trapped === 45);
  check('dead extraction', e.dead === 3);

  // 3. scoring monotonicity: more trapped -> higher score
  const r1 = parseReport('Flood at A, 10 people trapped', { timestamp: Date.now() - 60000, source: 'sms', reliability: 0.8 });
  const r2 = parseReport('Flood at B, 500 people trapped', { timestamp: Date.now() - 60000, source: 'sms', reliability: 0.8 });
  const s1 = scoreReport(r1).score, s2 = scoreReport(r2).score;
  check('severity monotonic with scale', s2 > s1, s1 + ' vs ' + s2);

  // 4. urgency decay: older report scores lower
  const fresh = parseReport('Fire at C, 20 injured', { timestamp: Date.now() - 60000, source: 'sms', reliability: 0.8 });
  const stale = parseReport('Fire at C, 20 injured', { timestamp: Date.now() - 72 * 3600000, source: 'sms', reliability: 0.8 });
  check('urgency decay over time', scoreReport(fresh).score > scoreReport(stale).score);

  // 5. priority ordering deterministic
  const now = Date.now();
  const plan1 = buildPlan(demoData(now), { now });
  const plan2 = buildPlan(demoData(now), { now });
  check('priority ordering deterministic', plan1.text === plan2.text);
  check('all demo reports ranked', plan1.scored.length === DEMO_REPORTS.length);

  // 6. CRITICAL level present in demo
  check('demo has critical events', plan1.critical >= 1, 'critical=' + plan1.critical);
  check('resources allocated to top', Array.isArray(plan1.scored[0].resources) && plan1.scored[0].resources.length > 0);

  // 7. HTML well-formed
  const h = htmlReport(plan1, { now });
  check('html well-formed', h.indexOf('<!DOCTYPE html>') === 0 && h.indexOf('</html>') > h.indexOf('<table'));
  const qt = parseReport("Building \"X\" collapsed near \"Main St\"", { timestamp: Date.now(), source: 'sms', reliability: 0.7 });
  const qh = htmlReport(buildPlan([qt], { now: Date.now() }), { now: Date.now() });
  check('html escapes quotes', qh.indexOf('&quot;') > -1);

  // 8. no forbidden markers (strip comments + strings, then scan)
  const code = fs.readFileSync(__filename, 'utf8');
  const stripped = code.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')
    .replace(/'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|`(?:[^`\\]|\\.)*`/g, '');
  const bad = /TODO|FIXME|debugger/.test(stripped);
  check('no forbidden markers in code', !bad);

  // 9. under 100KB
  check('under 100KB', Buffer.byteLength(code) <= 100 * 1024, Buffer.byteLength(code) + ' bytes');

  // 10. hash/PRNG sanity
  check('hashStr deterministic', hashStr('abc') === hashStr('abc') && hashStr('abc') !== hashStr('abd'));
  check('mulberry32 in [0,1)', (function () { const r = mulberry32(1); for (let i = 0; i < 100; i++) { const v = r(); if (v < 0 || v >= 1) return false; } return true; })());

  let pass = 0;
  for (const r of results) {
    if (r.pass) pass++; else console.log('FAIL: ' + r.name + (r.extra ? ' (' + r.extra + ')' : ''));
  }
  console.log('RescuePulse self-test: ' + pass + '/' + results.length + ' passed');
  return pass === results.length;
}

module.exports = {
  VERSION, parseReport, detectCategory, scoreReport, prioritize, buildPlan,
  htmlReport, demoData, run, selfTest, hashStr, mulberry32
};

if (require.main === module) {
  process.exit(run(process.argv));
}