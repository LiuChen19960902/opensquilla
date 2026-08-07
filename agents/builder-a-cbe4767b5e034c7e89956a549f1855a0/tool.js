'use strict';

/*
 * termflash v1.0.0
 * Zero-dependency spaced-repetition flashcard engine for the terminal.
 *
 * Single-file CommonJS tool for Node 18+. No third-party packages.
 * Includes an SM-2 style scheduler, interactive quiz sessions with
 * same-session re-queueing for forgotten cards, progress analytics with
 * a forgetting-curve chart, 7-day due-load forecast, mastery tracking,
 * built-in demo decks, JSON export, and a built-in self-test suite.
 *
 * Usage:
 *   node tool.js --init code                create a deck from a demo deck
 *   node tool.js --quiz                     interactive quiz session (TTY)
 *   node tool.js --review                   list cards due now (pipe friendly)
 *   node tool.js --stats                    progress dashboard
 *   node tool.js --forecast                 7-day due-load projection
 *   node tool.js --add "Q" "A" --tag js     append a flashcard
 *   node tool.js --export --out deck.json   export deck data
 *   node tool.js --self-test                run built-in checks
 */

const fs = require('fs');
const path = require('path');
const readline = require('readline');

const NAME = 'termflash';
const VERSION = '1.0.0';
const DAY_MS = 86400000;
const MIN_EF = 1.3;
const MAX_EF = 3.0;

/* ------------------------------- themes -------------------------------- */

const THEMES = {
  ocean:  { series: ['#38bdf8', '#818cf8', '#f472b6', '#34d399', '#fbbf24'], grid: '#64748b', accent: '#38bdf8' },
  neon:   { series: ['#22d3ee', '#a78bfa', '#f0abfc', '#4ade80', '#fde047'], grid: '#52525b', accent: '#22d3ee' },
  forest: { series: ['#4ade80', '#2dd4bf', '#a3e635', '#facc15', '#fb923c'], grid: '#3f6212', accent: '#4ade80' },
  sunset: { series: ['#fb923c', '#f472b6', '#c084fc', '#f87171', '#fbbf24'], grid: '#7c2d12', accent: '#fb923c' },
  mono:   { series: ['#e2e8f0', '#94a3b8', '#cbd5e1', '#64748b', '#f8fafc'], grid: '#475569', accent: '#e2e8f0' },
  candy:  { series: ['#f472b6', '#a78bfa', '#60a5fa', '#34d399', '#facc15'], grid: '#831843', accent: '#f472b6' },
};

function hex2rgb(h) {
  h = h.replace('#', '');
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

function rgb2ansi(r, g, b) {
  const table = [0, 95, 135, 175, 215, 255];
  let best = 16 + 36 * Math.round((r / 255) * 5) + 6 * Math.round((g / 255) * 5) + Math.round((b / 255) * 5);
  let bestD = 1e9;
  for (let i = 0; i < 6; i++) {
    for (let j = 0; j < 6; j++) {
      for (let k = 0; k < 6; k++) {
        const d = Math.abs(r - table[i]) + Math.abs(g - table[j]) + Math.abs(b - table[k]);
        if (d < bestD) { bestD = d; best = 16 + 36 * i + 6 * j + k; }
      }
    }
  }
  return bestD < 24 ? best : 16 + 36 * Math.round((r / 255) * 5) + 6 * Math.round((g / 255) * 5) + Math.round((b / 255) * 5);
}

class Palette {
  constructor(opts) {
    this.enabled = opts.color === null ? !!(process.stdout.isTTY) : !!opts.color;
    const t = THEMES[opts.theme] || THEMES.ocean;
    this.series = t.series;
    this.grid = t.grid;
    this.accent = t.accent;
  }
  fg(c) {
    if (!this.enabled) return '';
    const rgb = hex2rgb(c);
    return '\x1b[38;5;' + rgb2ansi(rgb[0], rgb[1], rgb[2]) + 'm';
  }
  reset() { return this.enabled ? '\x1b[0m' : ''; }
  bold(s) { return this.enabled ? '\x1b[1m' + s + '\x1b[0m' : s; }
  dim(s) { return this.enabled ? '\x1b[2m' + s + '\x1b[0m' : s; }
  color(s, i) {
    const c = this.series[i % this.series.length];
    return this.enabled ? this.fg(c) + s + this.reset() : s;
  }
  accentS(s) { return this.enabled ? this.fg(this.accent) + s + this.reset() : s; }
  gridS(s) { return this.enabled ? this.fg(this.grid) + s + this.reset() : s; }
}

/* ---------------------------- text helpers ----------------------------- */

function pad(n, s, ch) {
  s = String(s); ch = ch || ' ';
  while (s.length < n) s = ch + s;
  return s;
}

function padR(n, s, ch) {
  s = String(s); ch = ch || ' ';
  while (s.length < n) s += ch;
  return s;
}

function round2(x) { return Math.round(x * 100) / 100; }

function startOfDay(ts) {
  const d = new Date(ts);
  d.setUTCHours(0, 0, 0, 0);
  return d.getTime();
}

function fmtDate(ts) {
  const d = new Date(ts);
  return d.getUTCFullYear() + '-' + pad(2, d.getUTCMonth() + 1, '0') + '-' + pad(2, d.getUTCDate(), '0');
}

function fmtRel(ts, now) {
  const diff = ts - now;
  if (diff <= -2 * DAY_MS) return Math.floor(-diff / DAY_MS) + 'd ago';
  if (diff < 0) return 'today';
  if (diff < DAY_MS) return Math.max(1, Math.round(diff / 3600000)) + 'h';
  return Math.round(diff / DAY_MS) + 'd';
}

function fmtInt(days) {
  if (days <= 0) return '<1d';
  if (days < 30) return days + 'd';
  const mo = Math.round(days / 30);
  if (mo < 12) return mo + 'mo';
  return Math.round(days / 365) + 'y';
}

function wrapText(text, width) {
  const words = String(text).split(/\s+/);
  const lines = [];
  let cur = '';
  words.forEach((w) => {
    const next = (cur + ' ' + w).trim();
    if (next.length > width) {
      if (cur) lines.push(cur.trim());
      cur = w;
    } else {
      cur = next;
    }
  });
  if (cur) lines.push(cur.trim());
  return lines.length ? lines : [''];
}

/* ----------------------------- demo decks ------------------------------ */

const DEMO_DECKS = {
  code: {
    name: 'Programming Fundamentals',
    cards: [
      { q: 'What does HTTP status 200 mean?', a: 'OK — the request succeeded.', tag: 'web' },
      { q: 'What does HTTP status 404 mean?', a: 'Not Found — the resource does not exist.', tag: 'web' },
      { q: 'What is a hash table?', a: 'A structure mapping keys to values via a hash function, giving O(1) average lookup.', tag: 'data-structures' },
      { q: 'What is Big-O notation?', a: 'A measure of how runtime or memory grows with input size, e.g. O(n), O(log n).', tag: 'algorithms' },
      { q: 'What is a closure?', a: 'A function that retains access to its lexical scope even after the outer function returns.', tag: 'js' },
      { q: 'What is recursion?', a: 'A function that calls itself on a smaller sub-problem until a base case is hit.', tag: 'algorithms' },
      { q: 'What is a stack?', a: 'A LIFO data structure — last in, first out. Push/pop are O(1).', tag: 'data-structures' },
      { q: 'What is a queue?', a: 'A FIFO data structure — first in, first out.', tag: 'data-structures' },
      { q: 'What does SQL stand for?', a: 'Structured Query Language.', tag: 'databases' },
      { q: 'What is a database index?', a: 'A structure that speeds up lookups at the cost of write time and storage.', tag: 'databases' },
      { q: 'What is a binary search tree?', a: 'A tree where each node has at most two children; left < node < right.', tag: 'data-structures' },
      { q: 'What is the time complexity of merge sort?', a: 'O(n log n) in all cases.', tag: 'algorithms' },
      { q: 'What is the difference between == and === in JS?', a: '=== compares value and type; == coerces types before comparing.', tag: 'js' },
      { q: 'What is a git merge conflict?', a: 'When two branches changed the same lines and git cannot auto-merge them.', tag: 'git' },
    ],
  },
  lang: {
    name: 'Mandarin Basics',
    cards: [
      { q: 'How do you say "hello" in Mandarin?', a: '你好 (nǐ hǎo)', tag: 'greetings' },
      { q: 'How do you say "thank you"?', a: '谢谢 (xiè xie)', tag: 'greetings' },
      { q: 'How do you say "sorry / excuse me"?', a: '对不起 (duì bu qǐ) or 不好意思 (bù hǎo yì si)', tag: 'greetings' },
      { q: 'How do you say "goodbye"?', a: '再见 (zài jiàn)', tag: 'greetings' },
      { q: 'How do you count 1 to 5?', a: '一 二 三 四 五 (yī èr sān sì wǔ)', tag: 'numbers' },
      { q: 'How do you say "yes" and "no"?', a: '是 (shì) and 不 (bù); often 对 (duì) for "correct".', tag: 'basics' },
      { q: 'How do you ask "what is this?"', a: '这是什么？(zhè shì shén me?)', tag: 'questions' },
      { q: 'How do you say "I love learning"?', a: '我爱学习 (wǒ ài xué xí)', tag: 'phrases' },
      { q: 'What does 大 (dà) mean?', a: 'Big / large.', tag: 'adjectives' },
      { q: 'What does 小 (xiǎo) mean?', a: 'Small / little.', tag: 'adjectives' },
      { q: 'How do you say "please" (inviting someone in)?', a: '请 (qǐng), e.g. 请进 qǐng jìn — please come in.', tag: 'greetings' },
      { q: 'How do you ask "how are you?"', a: '你好吗？(nǐ hǎo ma?) or 最近怎么样？(zuì jìn zěn me yàng?)', tag: 'questions' },
    ],
  },
};

/* ---------------------------- SM-2 scheduler --------------------------- */

const RATING_LABELS = { 1: 'Again', 2: 'Hard', 3: 'Good', 4: 'Easy' };
const RATING_KEYS = { a: 1, again: 1, h: 2, hard: 2, g: 3, good: 3, e: 4, easy: 4 };
const PRAISE = ['Nicely done!', 'Crisp recall.', 'On fire.', 'Solid.', 'You know this.', 'Smooth.'];
const GENTLE = ['No worries — the curve will catch it.', 'Recall wobbles are normal.', 'Repeating builds it.', 'Almost — see you soon.'];

function schedule(card, rating, now) {
  const c = {
    id: card.id,
    q: card.q,
    a: card.a,
    tag: card.tag,
    status: card.status,
    ef: card.ef,
    interval: card.interval,
    reps: card.reps,
    lapses: card.lapses,
    due: card.due,
    last: card.last,
    history: (card.history || []).slice(-39),
  };
  let interval = c.interval;
  if (rating === 1) {
    c.reps = 0;
    c.lapses += 1;
    c.ef = Math.max(MIN_EF, round2(c.ef - 0.20));
    interval = 0;
  } else if (rating === 2) {
    interval = c.reps === 0 ? 1 : Math.max(1, Math.round(c.interval * 1.2));
    c.ef = Math.max(MIN_EF, round2(c.ef - 0.15));
    c.reps += 1;
  } else if (rating === 3) {
    interval = c.reps === 0 ? 1 : Math.max(1, Math.round(c.interval * c.ef));
    c.reps += 1;
  } else {
    interval = c.reps === 0 ? 4 : Math.max(1, Math.round(c.interval * c.ef * 1.3));
    c.ef = Math.min(MAX_EF, round2(c.ef + 0.15));
    c.reps += 1;
  }
  c.interval = interval;
  c.due = rating === 1 ? now + 10 * 60000 : now + interval * DAY_MS;
  c.last = now;
  c.status = rating === 1 ? 'learning' : (interval >= 21 ? 'mature' : 'learning');
  c.history.push({ t: now, rating: rating, interval: interval });
  return c;
}

/* ------------------------------ storage -------------------------------- */

function defaultDataPath() {
  return path.join(process.cwd(), 'termflash.data.json');
}

function loadDeck(file) {
  if (!fs.existsSync(file)) return null;
  try {
    const d = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (!d || !Array.isArray(d.cards)) return null;
    return d;
  } catch (e) {
    return null;
  }
}

function saveDeck(deck, file) {
  deck.updated = Date.now();
  const tmp = file + '.' + process.pid + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(deck, null, 2));
  fs.renameSync(tmp, file);
}

function makeDeckFromDemo(key) {
  const demo = DEMO_DECKS[key] || DEMO_DECKS.code;
  const now = Date.now();
  const cards = demo.cards.map((c, i) => ({
    id: key + '-' + (i + 1),
    q: c.q,
    a: c.a,
    tag: c.tag || 'general',
    status: 'new',
    ef: 2.5,
    interval: 0,
    reps: 0,
    lapses: 0,
    due: now,
    last: null,
    history: [],
  }));
  return {
    tool: NAME,
    version: VERSION,
    deck: demo.name,
    key: key,
    created: now,
    updated: now,
    cards: cards,
  };
}

/* ------------------------------- stats --------------------------------- */

function dayIndex(ts) { return Math.floor(ts / DAY_MS); }

function streakOf(deck, now) {
  const set = new Set();
  deck.cards.forEach((c) => (c.history || []).forEach((h) => set.add(dayIndex(h.t))));
  let n = 0;
  for (let i = 0; i < 3650; i++) {
    if (set.has(dayIndex(now) - i)) n += 1;
    else break;
  }
  return n;
}

function bestStreak(deck) {
  const set = new Set();
  deck.cards.forEach((c) => (c.history || []).forEach((h) => set.add(dayIndex(h.t))));
  const days = Array.from(set).sort((a, b) => a - b);
  let best = 0;
  let run = 0;
  days.forEach((d, i) => {
    run = i > 0 && d === days[i - 1] + 1 ? run + 1 : 1;
    if (run > best) best = run;
  });
  return best;
}

function xpOf(deck) {
  let xp = 0;
  deck.cards.forEach((c) => {
    xp += (c.history || []).length * 10;
    if (c.interval >= 21) xp += 50;
    if (c.reps >= 5) xp += 20;
  });
  return xp;
}

function levelOf(xp) {
  return Math.min(10, Math.floor(Math.log2(xp / 50 + 1)));
}

function stars(level) {
  return '★'.repeat(level) + '☆'.repeat(10 - level);
}

function masteryOf(c) {
  if (c.reps === 0) return 'New';
  if (c.interval === 0) return 'Relearning';
  if (c.interval < 7) return 'Developing';
  if (c.interval < 21) return 'Established';
  return 'Mastered';
}

function computeStats(deck, now) {
  const s = {
    total: deck.cards.length,
    byStatus: { new: 0, learning: 0, mature: 0 },
    due: 0,
    learned: 0,
    reviews: 0,
    goodReviews: 0,
    againReviews: 0,
    sumEf: 0,
    sumInt: 0,
    tags: {},
    studyDays: 0,
  };
  const daySet = new Set();
  deck.cards.forEach((c) => {
    s.byStatus[c.status] = (s.byStatus[c.status] || 0) + 1;
    if (c.due <= now) s.due += 1;
    if (c.reps > 0) s.learned += 1;
    s.sumEf += c.ef;
    s.sumInt += c.interval;
    const t = c.tag || 'general';
    s.tags[t] = (s.tags[t] || 0) + 1;
    (c.history || []).forEach((h) => {
      s.reviews += 1;
      if (h.rating >= 3) s.goodReviews += 1;
      else s.againReviews += 1;
      daySet.add(dayIndex(h.t));
    });
  });
  s.avgEf = s.total ? round2(s.sumEf / s.total) : 0;
  s.avgInt = s.total ? Math.round(s.sumInt / s.total) : 0;
  s.retention = s.reviews ? Math.round((s.goodReviews / s.reviews) * 100) : null;
  s.studyDays = daySet.size;
  return s;
}

function buildQueue(deck, now, limit) {
  const due = deck.cards.filter((c) => c.due <= now).sort((x, y) => x.due - y.due);
  const fresh = deck.cards.filter((c) => c.due > now && c.reps === 0);
  const n = Math.max(0, limit - due.length);
  return due.concat(fresh.slice(0, n));
}

/* ------------------------------ charts --------------------------------- */

function retentionChart(deck, now, days, P) {
  const out = [P.bold('Retention (last ' + days + ' days)')];
  let rows = 0;
  for (let d = days - 1; d >= 0; d--) {
    const dayStart = startOfDay(now) - d * DAY_MS;
    const dayEnd = dayStart + DAY_MS;
    let tot = 0;
    let good = 0;
    deck.cards.forEach((c) => (c.history || []).forEach((h) => {
      if (h.t >= dayStart && h.t < dayEnd) {
        tot += 1;
        if (h.rating >= 3) good += 1;
      }
    }));
    if (tot === 0) continue;
    rows += 1;
    const pct = Math.round((good / tot) * 100);
    const n = Math.round((good / tot) * 16);
    out.push(' ' + P.dim(fmtDate(dayStart)) + ' ' + P.color('█'.repeat(n), 10 - d) + P.dim('░'.repeat(16 - n)) + ' ' + pct + '% (' + good + '/' + tot + ')');
  }
  if (rows === 0) out.push(P.dim('  no reviews in this window yet'));
  return out;
}

function masteryChart(deck, P) {
  const counts = {};
  deck.cards.forEach((c) => {
    const m = masteryOf(c);
    counts[m] = (counts[m] || 0) + 1;
  });
  const order = ['New', 'Relearning', 'Developing', 'Established', 'Mastered'];
  const max = Math.max(1, order.map((m) => counts[m] || 0));
  const out = [P.bold('Mastery distribution')];
  order.forEach((m, i) => {
    const v = counts[m] || 0;
    const n = Math.round((v / max) * 20);
    out.push(' ' + padR(12, m) + ' ' + P.color('█'.repeat(n), i) + P.dim('░'.repeat(20 - n)) + ' ' + v);
  });
  return out;
}

function forecastBuckets(deck, now, days) {
  const b = new Array(days).fill(0);
  deck.cards.forEach((c) => {
    if (c.due <= now) {
      b[0] += 1;
      return;
    }
    const sim = schedule(c, 3, now);
    const diff = Math.max(0, Math.ceil((startOfDay(sim.due) - startOfDay(now)) / DAY_MS));
    if (diff < days) b[diff] += 1;
  });
  return b;
}

function forecastChart(deck, now, days, P) {
  const b = forecastBuckets(deck, now, days);
  const max = Math.max(1, b);
  const labels = ['Today'];
  for (let i = 1; i < days; i++) labels.push('+' + i + 'd');
  const out = [P.bold('Forecast — projected due load (if rated Good)')];
  b.forEach((v, i) => {
    const n = Math.round((v / max) * 20);
    out.push(' ' + padR(5, labels[i]) + ' ' + P.color('█'.repeat(n), i) + P.dim('░'.repeat(20 - n)) + ' ' + v);
  });
  out.push(P.dim(' total due in window: ' + b.reduce((a, x) => a + x, 0) + ' cards'));
  return out;
}

/* ------------------------------ rendering ------------------------------ */

function box(title, bodyLines, width, P, colorIdx) {
  const w = Math.max(width, 24);
  const padN = Math.max(1, w - title.length - 4);
  const top = P.color('┌─ ' + title, colorIdx) + P.dim('─'.repeat(padN)) + P.color('─┐', colorIdx);
  const bottom = P.color('└', colorIdx) + P.dim('─'.repeat(w - 2)) + P.color('┘', colorIdx);
  const lines = bodyLines.map((l) => ' ' + P.dim('│ ') + padR(w - 4, l) + ' ' + P.dim('│'));
  return [top].concat(lines, [bottom]).join('\n');
}

function renderReview(deck, now, opts) {
  const P = new Palette(opts);
  const q = buildQueue(deck, now, opts.limit || 20);
  const out = [];
  out.push(P.bold(NAME + ' · review · ' + deck.deck) + P.dim(' · ' + q.length + ' cards due'));
  q.forEach((c, i) => {
    out.push('');
    out.push(P.color('Q' + pad(2, String(i + 1)), i) + ' ' + P.bold(c.q));
    out.push(P.gridS('   A: ') + c.a);
    out.push(P.dim('   next if rated → Again 10min · Hard ' + fmtInt(Math.max(1, Math.round(c.interval * 1.2))) + ' · Good ' + fmtInt(Math.max(1, Math.round(c.interval * (c.ef || 2.5)))) + ' · Easy ' + fmtInt(Math.max(1, Math.round(c.interval * (c.ef || 2.5) * 1.3)))));
  });
  if (q.length === 0) out.push(P.dim('No cards due right now — enjoy the break.'));
  out.push('');
  out.push(P.dim(NAME + ' v' + VERSION + ' · ' + q.length + ' due · run --quiz for an interactive session'));
  return out.join('\n');
}

function renderStats(deck, now, opts) {
  const P = new Palette(opts);
  const s = computeStats(deck, now);
  const width = Math.min(100, Math.max(50, process.stdout.columns || 80));
  const xp = xpOf(deck);
  const lvl = levelOf(xp);
  const out = [];
  out.push(P.bold(NAME + ' · stats · ' + deck.deck));
  out.push(P.gridS('─'.repeat(width)));
  out.push(' Cards      ' + s.total + '  ' + P.dim('(' + s.byStatus.new + ' new · ' + s.byStatus.learning + ' learning · ' + s.byStatus.mature + ' mature)'));
  out.push(' Due now    ' + s.due + P.dim(' (' + Math.round((s.due / Math.max(1, s.total)) * 100) + '%)'));
  out.push(' Learned    ' + s.learned + P.dim(' of ' + s.total));
  out.push(' Reviews    ' + s.reviews + P.dim(' · retention ' + (s.retention === null ? '—' : s.retention + '%')));
  out.push(' Avg ease   ' + s.avgEf + P.dim(' · avg interval ' + fmtInt(s.avgInt)));
  out.push(' Study days ' + s.studyDays + P.dim(' · streak ' + streakOf(deck, now) + 'd') + P.dim(' · best ' + bestStreak(deck) + 'd'));
  out.push(' Level      ' + lvl + '/10 ' + P.color(stars(lvl), 1) + P.dim(' · ' + xp + ' XP'));
  out.push('');
  out.push.apply(out, retentionChart(deck, now, 14, P));
  out.push('');
  out.push.apply(out, masteryChart(deck, P));
  out.push('');
  out.push.apply(out, forecastChart(deck, now, 7, P));
  out.push('');
  out.push(' Tags       ' + Object.keys(s.tags).sort().map((t) => P.color(t + ':' + s.tags[t], 0)).join('  '));
  out.push(P.dim(NAME + ' v' + VERSION + ' · data ' + (opts.data || defaultDataPath())));
  return out.join('\n');
}

/* ------------------------------- quiz ---------------------------------- */

function askLine(rl, prompt) {
  return new Promise((resolve) => rl.question(prompt, resolve));
}

function makeLineReader() {
  // For piped (non-TTY) stdin, read all input up front so the quiz is
  // deterministic and CI-friendly; for a real terminal use readline.
  if (!process.stdin.isTTY) {
    const fs = require('fs');
    const data = fs.readFileSync(0, 'utf8').split('\n');
    let i = 0;
    return {
      next: function (prompt) {
        if (prompt) process.stdout.write(prompt);
        const v = i < data.length ? data[i] : '';
        i += 1;
        return Promise.resolve(v.trim());
      },
      close: function () {},
    };
  }
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  return {
    next: function (prompt) { return new Promise((resolve) => rl.question(prompt, resolve)); },
    close: function () { rl.close(); },
  };
}

async function runQuiz(deck, opts) {
  const P = new Palette(opts);
  opts = Object.assign({}, opts, { data: opts.data || defaultDataPath() });
  const now = Date.now();
  const queue = buildQueue(deck, now, opts.limit || 20);
  if (queue.length === 0) {
    console.log(P.bold('No cards due right now — nicely done.'));
    console.log(P.dim('Run --stats for the full picture, or --add "Q" "A" to grow your deck.'));
    return { done: 0, good: 0, again: 0 };
  }
  const rl = makeLineReader();
  const width = Math.min(100, Math.max(50, process.stdout.columns || 80));
  let idx = 0;
  let good = 0;
  let again = 0;
  let skipped = 0;
  const requeued = {};
  const t0 = Date.now();
  for (let qi = 0; qi < queue.length; qi++) {
    const card = queue[qi];
    idx += 1;
    console.log('');
    console.log(box('Question ' + idx + '/' + queue.length + ' · ' + (card.tag || 'general'), wrapText(card.q, width - 8), width, P, idx - 1));
    console.log(P.dim('   due ' + fmtRel(card.due, Date.now()) + ' · reps ' + card.reps + ' · ease ' + card.ef + ' · ' + masteryOf(card)));
    await rl.next(P.dim('   Press Enter to reveal answer… '));
    console.log(box('Answer', wrapText(card.a, width - 8), width, P, 2));
    console.log('   ' + P.dim('[1] Again  [2] Hard  [3] Good  [4] Easy  (a/h/g/e)'));
    let rating = null;
    while (rating === null) {
      const raw = (await rl.next('   Your rating > ')).trim().toLowerCase();
      if (raw === 'q' || raw === 'quit') {
        skipped += 1;
        break;
      }
      if (raw === '') {
        console.log(P.dim('   (empty input treated as skip — type 1-4 or a/h/g/e)'));
        skipped += 1;
        break;
      }
      if (/^[1-4]$/.test(raw)) rating = parseInt(raw, 10);
      else if (RATING_KEYS[raw] !== undefined) rating = RATING_KEYS[raw];
      else console.log(P.dim('   Hmm — type 1-4 or a/h/g/e.'));
    }
    if (rating === null) continue;
    const updated = schedule(card, rating, Date.now());
    const i = deck.cards.findIndex((c) => c.id === card.id);
    if (i >= 0) deck.cards[i] = updated;
    saveDeck(deck, opts.data);
    if (rating >= 3) good += 1;
    else again += 1;
    const msg = rating >= 3 ? PRAISE[updated.history.length % PRAISE.length] : GENTLE[updated.lapses % GENTLE.length];
    console.log('   ' + (rating >= 3 ? P.color('✓ ' + msg, 3) : P.color('↻ ' + msg, 1)) + P.dim(' · next in ' + fmtInt(updated.interval || 0) + ' (' + RATING_LABELS[rating] + ')'));
    if (rating === 1 && !requeued[card.id]) {
      requeued[card.id] = true;
      queue.push(card);
    }
  }
  rl.close();
  const secs = Math.max(1, Math.round((Date.now() - t0) / 1000));
  const answered = idx - skipped;
  const pct = answered ? Math.round((good / answered) * 100) : 0;
  console.log('');
  console.log(P.bold('Session complete'));
  console.log('  answered ' + answered + ' · ' + pct + '% recalled · ' + secs + 's · ' + good + ' good · ' + again + ' again' + (skipped ? ' · ' + skipped + ' skipped' : ''));
  console.log('  ' + P.dim('streak ' + streakOf(deck, Date.now()) + 'd · run --stats for the full picture'));
  return { done: answered, good: good, again: again };
}

/* -------------------------------- CLI ---------------------------------- */

function parseArgs(argv) {
  const opts = {
    deck: 'code',
    data: null,
    action: null,
    theme: 'ocean',
    color: null,
    limit: 20,
    tag: 'general',
    out: null,
    addQ: null,
    addA: null,
    force: false,
    forceTty: false,
  };
  const positional = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const take = () => {
      if (i + 1 >= argv.length) throw new Error('missing value for ' + a);
      return argv[++i];
    };
    if (a === '--help' || a === '-h') opts.action = 'help';
    else if (a === '--version' || a === '-v') opts.action = 'version';
    else if (a === '--self-test') opts.action = 'self-test';
    else if (a === '--init') opts.action = 'init';
    else if (a === '--quiz') opts.action = 'quiz';
    else if (a === '--review') opts.action = 'review';
    else if (a === '--stats') opts.action = 'stats';
    else if (a === '--forecast') opts.action = 'forecast';
    else if (a === '--export') opts.action = 'export';
    else if (a === '--add') opts.action = 'add';
    else if (a === '--deck') opts.deck = take();
    else if (a === '--data') opts.data = take();
    else if (a === '--theme') opts.theme = take();
    else if (a === '--limit') {
      const v = parseInt(take(), 10);
      opts.limit = Number.isFinite(v) && v > 0 ? v : 20;
    } else if (a === '--tag') opts.tag = take();
    else if (a === '--out') opts.out = take();
    else if (a === '--no-color') opts.color = false;
    else if (a === '--color') opts.color = true;
    else if (a === '--force') opts.force = true;
    else if (a === '--force-tty') opts.forceTty = true;
    else if (a.startsWith('-')) throw new Error('unknown flag: ' + a);
    else positional.push(a);
  }
  if (positional.length >= 1) opts.addQ = positional[0];
  if (positional.length >= 2) opts.addA = positional[1];
  return opts;
}

function printHelp() {
  console.log(NAME + ' v' + VERSION + ' — spaced-repetition flashcards in your terminal');
  console.log('');
  console.log('USAGE');
  console.log('  node tool.js <action> [options]');
  console.log('');
  console.log('ACTIONS');
  console.log('  --quiz            interactive quiz (due cards first, then fresh; forgotten cards re-queue)');
  console.log('  --review          list cards due now (pipe-friendly, no interaction)');
  console.log('  --stats           dashboard: retention curve, mastery, forecast, streak, XP');
  console.log('  --forecast        7-day projected due load');
  console.log('  --init [deck]     create a deck file from a demo deck (code|lang, default code)');
  console.log('  --add "Q" "A"     append a flashcard (--tag optional)');
  console.log('  --export          dump deck JSON to stdout or --out <file>');
  console.log('  --self-test       run the built-in test suite');
  console.log('  --help, --version');
  console.log('');
  console.log('OPTIONS');
  console.log('  --deck code|lang  demo deck to seed when no data file exists (default code)');
  console.log('  --data <path>     deck file path (default ./termflash.data.json)');
  console.log('  --theme <name>    ocean|neon|forest|sunset|mono|candy');
  console.log('  --limit <n>       max cards per session (default 20)');
  console.log('  --tag <name>      tag for --add (default general)');
  console.log('  --out <file>      output file for --export');
  console.log('  --no-color / --color');
  console.log('  --force           overwrite existing deck file on --init');
  console.log('  --force-tty       allow --quiz even when stdin is not a TTY (CI/testing)');
  console.log('');
  console.log('EXAMPLES');
  console.log('  node tool.js --init code');
  console.log('  node tool.js --quiz --theme neon');
  console.log('  node tool.js --review | less -R');
  console.log('  node tool.js --add "What is a closure?" "A function that keeps its lexical scope" --tag js');
  console.log('  node tool.js --stats');
  console.log('  node tool.js --self-test');
  console.log('');
  console.log('During a quiz: press Enter to reveal the answer, then rate the card.');
  console.log('Ratings: 1 Again (+10 min) · 2 Hard (×1.2) · 3 Good (×ease) · 4 Easy (×ease×1.3)');
  console.log('Progress is saved to the deck file after every card.');
}

/* ----------------------------- self-test ------------------------------- */

function runSelfTest() {
  const NOW = Date.UTC(2026, 6, 7, 12, 0, 0);
  const base = {
    id: 'x', q: 'q', a: 'a', tag: 't', status: 'learning',
    ef: 2.5, interval: 1, reps: 1, lapses: 0, due: NOW, last: null, history: [],
  };
  const tests = [];
  const t = (name, fn) => {
    try {
      const info = fn();
      tests.push({ name: name, pass: true, info: info });
    } catch (e) {
      tests.push({ name: name, pass: false, info: e.message });
    }
  };

  t('sm2 again resets interval and increments lapses', () => {
    const c = Object.assign({}, base, { interval: 5, reps: 2 });
    const n = schedule(c, 1, NOW);
    if (n.interval !== 0) throw new Error('interval=' + n.interval);
    if (n.reps !== 0) throw new Error('reps=' + n.reps);
    if (n.lapses !== 1) throw new Error('lapses=' + n.lapses);
    if (n.ef !== 2.3) throw new Error('ef=' + n.ef);
    if (n.due !== NOW + 600000) throw new Error('due=' + n.due);
    return 'interval 0 · due +10min';
  });

  t('sm2 good grows interval by ease factor', () => {
    const c = Object.assign({}, base, { interval: 4, reps: 3, ef: 2.5 });
    const n = schedule(c, 3, NOW);
    if (n.interval !== 10) throw new Error('interval=' + n.interval);
    if (n.ef !== 2.5) throw new Error('ef=' + n.ef);
    return '4d → 10d';
  });

  t('sm2 easy boosts ease factor, capped at 3.0', () => {
    const c = Object.assign({}, base, { ef: 2.9 });
    const n = schedule(c, 4, NOW);
    if (n.ef !== 3.0) throw new Error('ef=' + n.ef);
    return 'ef 2.9 → 3.0';
  });

  t('sm2 hard lowers ease factor, floored at 1.3', () => {
    const c = Object.assign({}, base, { ef: 1.35 });
    const n = schedule(c, 2, NOW);
    if (n.ef !== 1.3) throw new Error('ef=' + n.ef);
    return 'ef 1.35 → 1.3';
  });

  t('schedule is deterministic for same inputs', () => {
    const c = Object.assign({}, base, { interval: 3, reps: 2, ef: 2.4 });
    const a = schedule(c, 3, NOW);
    const b = schedule(c, 3, NOW);
    if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error('mismatch');
    return 'identical';
  });

  t('demo decks are non-empty and well-formed', () => {
    const keys = Object.keys(DEMO_DECKS);
    if (keys.length < 2) throw new Error('need 2+ demo decks');
    keys.forEach((k) => {
      const d = DEMO_DECKS[k];
      if (!d.cards || d.cards.length < 10) throw new Error(k + ' cards=' + (d.cards || []).length);
      d.cards.forEach((c) => {
        if (!c.q || !c.a) throw new Error('card missing q/a in ' + k);
      });
    });
    return 'code=' + DEMO_DECKS.code.cards.length + ' · lang=' + DEMO_DECKS.lang.cards.length;
  });

  t('makeDeckFromDemo produces due-now new cards', () => {
    const d = makeDeckFromDemo('code');
    if (d.cards.length !== DEMO_DECKS.code.cards.length) throw new Error('count');
    const allNew = d.cards.every((c) => c.status === 'new' && c.ef === 2.5 && c.interval === 0 && c.reps === 0 && c.due === d.created);
    if (!allNew) throw new Error('not all new');
    return d.cards.length + ' cards';
  });

  t('buildQueue puts due cards first, then fresh', () => {
    const d = makeDeckFromDemo('code');
    const now = Date.now();
    const q = buildQueue(d, now, 5);
    if (q.length < 5) throw new Error('len=' + q.length);
    if (q[0].due > now) throw new Error('due card not first');
    return q.length + ' cards queued';
  });

  t('computeStats totals are consistent', () => {
    const d = makeDeckFromDemo('code');
    const s = computeStats(d, Date.now());
    if (s.total !== d.cards.length) throw new Error('total');
    if (s.byStatus.new + s.byStatus.learning + s.byStatus.mature !== s.total) throw new Error('status sum');
    if (s.due !== s.total) throw new Error('all new cards should be due');
    return s.total + ' total · ' + s.due + ' due';
  });

  t('retention rate after simulated reviews', () => {
    const d = makeDeckFromDemo('code');
    const now = Date.now();
    for (let i = 0; i < 4; i++) d.cards[i] = schedule(d.cards[i], 3, now);
    for (let i = 4; i < 6; i++) d.cards[i] = schedule(d.cards[i], 1, now);
    const s = computeStats(d, now);
    if (s.reviews !== 6) throw new Error('reviews=' + s.reviews);
    if (s.retention !== 67) throw new Error('retention=' + s.retention);
    return s.reviews + ' reviews · ' + s.retention + '%';
  });

  t('streak counts consecutive study days', () => {
    const d = makeDeckFromDemo('code');
    const now = Date.now();
    d.cards[0].history = [
      { t: now - 3 * DAY_MS, rating: 3, interval: 1 },
      { t: now - DAY_MS, rating: 3, interval: 1 },
      { t: now, rating: 3, interval: 1 },
    ];
    d.cards[0].reps = 3;
    if (streakOf(d, now) !== 2) throw new Error('streak=' + streakOf(d, now));
    if (bestStreak(d) !== 2) throw new Error('best=' + bestStreak(d));
    return 'current 2d · best 2d';
  });

  t('forecast buckets sum to total cards', () => {
    const d = makeDeckFromDemo('code');
    const now = Date.now();
    const b = forecastBuckets(d, now, 7);
    if (b.length !== 7) throw new Error('len');
    if (b.reduce((a, x) => a + x, 0) !== d.cards.length) throw new Error('sum');
    if (b[0] !== d.cards.length) throw new Error('all new cards due today');
    return b.join(',');
  });

  t('mastery labels map intervals', () => {
    const mk = (interval, reps) => ({ interval: interval, reps: reps });
    if (masteryOf(mk(0, 0)) !== 'New') throw new Error('new');
    if (masteryOf(mk(0, 2)) !== 'Relearning') throw new Error('relearning');
    if (masteryOf(mk(3, 1)) !== 'Developing') throw new Error('developing');
    if (masteryOf(mk(14, 2)) !== 'Established') throw new Error('established');
    if (masteryOf(mk(30, 3)) !== 'Mastered') throw new Error('mastered');
    return '5 levels';
  });

  t('level and XP scale with reviews', () => {
    if (levelOf(0) !== 0) throw new Error('lvl0');
    if (levelOf(500) < levelOf(50)) throw new Error('not monotonic');
    if (stars(3).length !== 10) throw new Error('stars length');
    return 'xp 0→lvl0 · xp 500→lvl' + levelOf(500);
  });

  t('date helpers format sanely', () => {
    const ts = Date.UTC(2026, 6, 7, 12, 0, 0);
    if (fmtDate(startOfDay(ts)) !== '2026-07-07') throw new Error('fmt=' + fmtDate(startOfDay(ts)));
    if (fmtRel(ts, ts + 3 * DAY_MS) !== '3d ago') throw new Error('rel=' + fmtRel(ts, ts + 3 * DAY_MS));
    if (fmtRel(ts - 5 * DAY_MS, ts) !== '5d ago') throw new Error('rel2=' + fmtRel(ts - 5 * DAY_MS, ts));
    if (fmtInt(45) !== '2mo') throw new Error('int=' + fmtInt(45));
    return 'ok';
  });

  t('review output is non-empty and contains questions', () => {
    const d = makeDeckFromDemo('code');
    const out = renderReview(d, Date.now(), { limit: 5, theme: 'ocean', color: false, data: 'x' });
    if (!out.includes('HTTP')) throw new Error('missing question');
    if (out.length < 100) throw new Error('too short');
    return out.length + ' chars';
  });

  t('palette disables ANSI when color=false', () => {
    const P = new Palette({ color: false, theme: 'neon' });
    const s = P.bold('x') + P.color('y', 0) + P.dim('z');
    if (s.includes('\x1b')) throw new Error('ANSI leaked');
    return 'plain output';
  });

  t('wrapText breaks long lines without losing content', () => {
    const lines = wrapText('one two three four five', 8);
    if (lines.join(' ').replace(/\s+/g, ' ') !== 'one two three four five') throw new Error('content lost');
    if (lines.some((l) => l.length > 8)) throw new Error('line too long: ' + JSON.stringify(lines));
    return lines.length + ' lines';
  });

  let pass = 0;
  let fail = 0;
  console.log(NAME + ' self-test');
  tests.forEach((tst) => {
    if (tst.pass) {
      pass += 1;
      console.log('  ok   ' + tst.name + '  [' + tst.info + ']');
    } else {
      fail += 1;
      console.log('  FAIL ' + tst.name + '  [' + tst.info + ']');
    }
  });
  console.log('RESULT: ' + pass + '/' + tests.length + ' passed');
  return fail;
}

/* -------------------------------- main --------------------------------- */

function main() {
  let opts;
  try {
    opts = parseArgs(process.argv.slice(2));
  } catch (e) {
    console.error('error: ' + e.message);
    console.error('run with --help for usage');
    process.exit(1);
  }
  if (!opts.action) {
    printHelp();
    process.exit(1);
  }
  if (opts.action === 'help') {
    printHelp();
    process.exit(0);
  }
  if (opts.action === 'version') {
    console.log(NAME + ' v' + VERSION);
    process.exit(0);
  }
  if (opts.action === 'self-test') {
    const fails = runSelfTest();
    process.exit(fails > 0 ? 1 : 0);
  }

  const file = opts.data || defaultDataPath();
  try {
    if (opts.action === 'init') {
      if (fs.existsSync(file) && !opts.force) {
        console.error('error: ' + file + ' already exists (use --force to recreate)');
        process.exit(1);
      }
      const deckKey = (opts.addQ && DEMO_DECKS[opts.addQ]) ? opts.addQ : opts.deck;
      const deck = makeDeckFromDemo(deckKey);
      saveDeck(deck, file);
      console.log('created deck "' + deck.deck + '" with ' + deck.cards.length + ' cards → ' + file);
      process.exit(0);
    }

    let deck = loadDeck(file);
    if (!deck) {
      deck = makeDeckFromDemo(opts.deck);
      saveDeck(deck, file);
    }

    if (opts.action === 'add') {
      if (!opts.addQ) {
        console.error('error: --add requires "question" and "answer" arguments');
        process.exit(1);
      }
      const id = deck.key + '-' + (deck.cards.length + 1);
      deck.cards.push({
        id: id,
        q: opts.addQ,
        a: opts.addA || '',
        tag: opts.tag || 'general',
        status: 'new',
        ef: 2.5,
        interval: 0,
        reps: 0,
        lapses: 0,
        due: Date.now(),
        last: null,
        history: [],
      });
      saveDeck(deck, file);
      console.log('added card ' + id + ' → ' + file);
      process.exit(0);
    }

    if (opts.action === 'export') {
      const payload = JSON.stringify(deck, null, 2);
      if (opts.out) {
        fs.writeFileSync(opts.out, payload);
        console.log('exported → ' + opts.out);
      } else {
        console.log(payload);
      }
      process.exit(0);
    }

    const now = Date.now();
    if (opts.action === 'review') {
      console.log(renderReview(deck, now, opts));
      process.exit(0);
    }
    if (opts.action === 'forecast') {
      const P = new Palette(opts);
      console.log(forecastChart(deck, now, 7, P).join('\n'));
      process.exit(0);
    }
    if (opts.action === 'stats') {
      console.log(renderStats(deck, now, opts));
      process.exit(0);
    }
    if (opts.action === 'quiz') {
      if (!opts.forceTty && !(process.stdin.isTTY && process.stdout.isTTY)) {
        const P = new Palette(opts);
        console.log(P.dim('non-TTY input detected — showing review mode instead of the interactive quiz'));
        console.log(renderReview(deck, now, opts));
        process.exit(0);
      }
      runQuiz(deck, opts)
        .then(() => process.exit(0))
        .catch((e) => {
          console.error('error: ' + (e && e.message ? e.message : e));
          process.exit(2);
        });
      return;
    }
    console.error('error: unknown action');
    process.exit(1);
  } catch (e) {
    console.error('error: ' + (e && e.message ? e.message : e));
    process.exit(2);
  }
}

if (require.main === module) {
  main();
}

module.exports = {
  NAME: NAME,
  VERSION: VERSION,
  schedule: schedule,
  makeDeckFromDemo: makeDeckFromDemo,
  buildQueue: buildQueue,
  computeStats: computeStats,
  streakOf: streakOf,
  bestStreak: bestStreak,
  xpOf: xpOf,
  levelOf: levelOf,
  masteryOf: masteryOf,
  forecastBuckets: forecastBuckets,
  wrapText: wrapText,
  runQuiz: runQuiz,
  fmtDate: fmtDate,
  fmtInt: fmtInt,
  runSelfTest: runSelfTest,
};