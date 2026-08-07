#!/usr/bin/env node
/**
 * VitaFlow — Personal Health Command Center
 * Health & Wellness Tech track · Agentathon (https://agentathon.dev)
 *
 * Zero-dependency Node.js (CommonJS, Node 18+), single file, under 100KB.
 *
 * Modules:
 *   nutrition — meal macro/micro analysis against RDA references
 *   bmr       — BMR/TDEE calculator with goal calorie targets
 *   plan      — workout plan generator (goal x frequency x equipment)
 *   breathe   — guided breathing sessions (4-7-8 / box / coherent)
 *   sleep     — sleep quality analysis with efficiency score
 *   trend     — health metrics dashboard from CSV data
 *   report    — integrated wellness report with demo data
 *
 * Run:
 *   node tool.js --self-test
 *   node tool.js report --demo
 */
'use strict';

const VERSION = '2.1.0';

const DISCLAIMER = [
  'DISCLAIMER: VitaFlow provides general wellness estimates, not medical advice.',
  'Results are informational only and never replace a physician or dietitian.',
  'If you have a medical condition, take medication, or notice unusual changes,',
  'please consult a qualified healthcare professional before changing your routine.',
  'By using this tool you accept these terms.'
].join('\n');

/* ------------------------------------------------------------------ */
/* Reference values (adult general population, per day)               */
/* ------------------------------------------------------------------ */
const RDA = {
  kcal: 2000, protein: 50, carbs: 260, fat: 70,
  fiber: 30, sugar: 50, sodium: 2300,
  vitC: 100, calcium: 1000, iron: 8
};

/* ------------------------------------------------------------------ */
/* Small helpers                                                      */
/* ------------------------------------------------------------------ */
function round1(x) { return Math.round(x * 10) / 10; }
function round0(x) { return Math.round(x); }
function pad(n, w) { const s = String(n); return s.length >= w ? s : ' '.repeat(w - s.length) + s; }
function padR(n, w) { const s = String(n); return s.length >= w ? s : s + ' '.repeat(w - s.length); }
function pct(part, whole) { return whole <= 0 ? 0 : Math.round((part / whole) * 100); }
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
function bar(value, max, width) {
  const len = Math.max(0, Math.min(width, Math.round((value / max) * width)));
  return '#' .repeat(len) + '.' .repeat(Math.max(0, width - len));
}
function bold(s) { return s; }
function hr() { return '-'.repeat(56); }

/* ------------------------------------------------------------------ */
/* Validation helpers (Safety layer)                                  */
/* ------------------------------------------------------------------ */
function checkNumber(name, value, lo, hi, unit) {
  const n = Number(value);
  if (value === undefined || value === null || value === '' || Number.isNaN(n)) {
    return { ok: false, msg: name + ' is required (got ' + String(value) + ')' };
  }
  if (n < lo || n > hi) {
    return { ok: false, msg: name + ' must be between ' + lo + ' and ' + hi + (unit ? ' ' + unit : '') + ' (got ' + value + ')' };
  }
  return { ok: true, value: n };
}

function checkEnum(name, value, allowed) {
  if (allowed.indexOf(value) === -1) {
    return { ok: false, msg: name + ' must be one of: ' + allowed.join(', ') + ' (got ' + String(value) + ')' };
  }
  return { ok: true, value: value };
}

function checkSex(value) {
  const v = String(value || '').toLowerCase();
  if (v === 'm' || v === 'male') return { ok: true, value: 'male' };
  if (v === 'f' || v === 'female') return { ok: true, value: 'female' };
  return { ok: false, msg: 'sex must be male/m or female/f (got ' + String(value) + ')' };
}

const ACTIVITY_LEVELS = {
  sedentary: { label: 'Sedentary (desk job, little exercise)', factor: 1.2 },
  light:     { label: 'Lightly active (1-3 workouts/week)',     factor: 1.375 },
  moderate:  { label: 'Moderately active (3-5 workouts/week)',  factor: 1.55 },
  active:    { label: 'Very active (6-7 workouts/week)',        factor: 1.725 },
  athlete:   { label: 'Athlete (hard training 2x/day)',         factor: 1.9 }
};

function checkActivity(value) {
  const v = String(value || '').toLowerCase();
  if (ACTIVITY_LEVELS[v]) return { ok: true, value: v };
  const keys = Object.keys(ACTIVITY_LEVELS);
  if (v === '1.2') return { ok: true, value: 'sedentary' };
  if (v === '1.375') return { ok: true, value: 'light' };
  if (v === '1.55') return { ok: true, value: 'moderate' };
  if (v === '1.725') return { ok: true, value: 'active' };
  if (v === '1.9') return { ok: true, value: 'athlete' };
  return { ok: false, msg: 'activity must be one of: ' + keys.join(', ') + ' (got ' + String(value) + ')' };
}

/* ------------------------------------------------------------------ */
/* Food database (per serving; s = serving description)               */
/* Fields: n name, a aliases, s serving, kcal, p protein, c carbs,    */
/*         f fat, fiber, sugar, sodium, vitC, ca calcium, fe iron     */
/* ------------------------------------------------------------------ */
const FOODS = [
  { n: 'oatmeal', a: ['oats', 'porridge'], s: '40g dry', kcal: 150, p: 5, c: 27, f: 3, fiber: 4, sugar: 1, sodium: 0, vitC: 0, ca: 20, fe: 1.5 },
  { n: 'banana', a: ['bananas'], s: '1 medium (105g)', kcal: 105, p: 1.3, c: 27, f: 0.4, fiber: 3.1, sugar: 14, sodium: 1, vitC: 10, ca: 6, fe: 0.3 },
  { n: 'egg', a: ['eggs', 'egg boiled', 'egg fried'], s: '1 large', kcal: 72, p: 6.3, c: 0.4, f: 4.8, fiber: 0, sugar: 0.2, sodium: 71, vitC: 0, ca: 28, fe: 0.9 },
  { n: 'chicken breast', a: ['chicken', 'chicken breast cooked'], s: '100g cooked', kcal: 165, p: 31, c: 0, f: 3.6, fiber: 0, sugar: 0, sodium: 74, vitC: 0, ca: 15, fe: 1 },
  { n: 'white rice', a: ['rice', 'cooked rice'], s: '150g cooked', kcal: 205, p: 4.3, c: 45, f: 0.4, fiber: 0.6, sugar: 0.1, sodium: 2, vitC: 0, ca: 15, fe: 0.9 },
  { n: 'brown rice', a: ['brown rice cooked'], s: '150g cooked', kcal: 165, p: 3.9, c: 34, f: 1.3, fiber: 2.7, sugar: 0.5, sodium: 7, vitC: 0, ca: 12, fe: 0.7 },
  { n: 'broccoli', a: ['broccoli steamed'], s: '100g', kcal: 34, p: 2.8, c: 6.6, f: 0.4, fiber: 2.6, sugar: 1.7, sodium: 33, vitC: 89, ca: 47, fe: 0.7 },
  { n: 'milk', a: ['whole milk', 'cow milk'], s: '250ml', kcal: 152, p: 8, c: 12, f: 8, fiber: 0, sugar: 12, sodium: 105, vitC: 0, ca: 276, fe: 0 },
  { n: 'greek yogurt', a: ['yogurt', 'plain yogurt'], s: '150g', kcal: 100, p: 15, c: 6, f: 0.4, fiber: 0, sugar: 6, sodium: 53, vitC: 0, ca: 165, fe: 0.1 },
  { n: 'salmon', a: ['salmon fillet'], s: '100g cooked', kcal: 208, p: 20, c: 0, f: 13, fiber: 0, sugar: 0, sodium: 61, vitC: 0, ca: 15, fe: 0.5 },
  { n: 'avocado', a: ['avocado half'], s: '1/2 fruit', kcal: 120, p: 1.5, c: 6.4, f: 11, fiber: 5, sugar: 0.3, sodium: 5.5, vitC: 6, ca: 6, fe: 0.3 },
  { n: 'apple', a: ['apples'], s: '1 medium', kcal: 95, p: 0.5, c: 25, f: 0.3, fiber: 4.4, sugar: 19, sodium: 1.8, vitC: 8, ca: 11, fe: 0.2 },
  { n: 'whole wheat bread', a: ['bread', 'whole grain bread'], s: '2 slices', kcal: 160, p: 8, c: 26, f: 2, fiber: 4, sugar: 3, sodium: 320, vitC: 0, ca: 60, fe: 1.4 },
  { n: 'peanut butter', a: ['peanut butter 2tbsp'], s: '32g', kcal: 188, p: 8, c: 7, f: 16, fiber: 3, sugar: 3, sodium: 147, vitC: 0, ca: 17, fe: 0.6 },
  { n: 'almonds', a: ['almond'], s: '28g (23 nuts)', kcal: 164, p: 6, c: 6, f: 14, fiber: 3.5, sugar: 1.2, sodium: 0, vitC: 0, ca: 76, fe: 1 },
  { n: 'sweet potato', a: ['sweet potato baked'], s: '130g', kcal: 112, p: 2, c: 26, f: 0.1, fiber: 3.9, sugar: 5.4, sodium: 73, vitC: 3, ca: 43, fe: 0.8 },
  { n: 'spinach', a: ['spinach raw'], s: '100g', kcal: 23, p: 2.9, c: 3.6, f: 0.4, fiber: 2.2, sugar: 0.4, sodium: 79, vitC: 28, ca: 99, fe: 2.7 },
  { n: 'tofu', a: ['tofu firm'], s: '100g', kcal: 76, p: 8, c: 1.9, f: 4.8, fiber: 0.3, sugar: 0.6, sodium: 7, vitC: 0, ca: 350, fe: 5.4 },
  { n: 'beef', a: ['beef steak', 'lean beef'], s: '100g cooked', kcal: 217, p: 26, c: 0, f: 12, fiber: 0, sugar: 0, sodium: 55, vitC: 0, ca: 14, fe: 2.6 },
  { n: 'turkey breast', a: ['turkey', 'turkey breast cooked'], s: '100g cooked', kcal: 135, p: 30, c: 0, f: 1, fiber: 0, sugar: 0, sodium: 54, vitC: 0, ca: 12, fe: 1 },
  { n: 'pasta', a: ['spaghetti cooked'], s: '150g cooked', kcal: 221, p: 8, c: 43, f: 1.3, fiber: 2.5, sugar: 0.8, sodium: 1, vitC: 0, ca: 10, fe: 1.7 },
  { n: 'cheddar cheese', a: ['cheese'], s: '28g', kcal: 113, p: 6.4, c: 0.9, f: 9.3, fiber: 0, sugar: 0.1, sodium: 174, vitC: 0, ca: 202, fe: 0.2 },
  { n: 'orange', a: ['oranges'], s: '1 medium', kcal: 62, p: 1.2, c: 15, f: 0.2, fiber: 3.1, sugar: 12, sodium: 0, vitC: 70, ca: 52, fe: 0.1 },
  { n: 'strawberries', a: ['strawberry'], s: '150g', kcal: 49, p: 1, c: 12, f: 0.5, fiber: 3, sugar: 7, sodium: 1, vitC: 89, ca: 22, fe: 0.6 },
  { n: 'blueberries', a: ['blueberry'], s: '150g', kcal: 85, p: 1.1, c: 21, f: 0.5, fiber: 3.6, sugar: 15, sodium: 1.5, vitC: 15, ca: 9, fe: 0.4 },
  { n: 'quinoa', a: ['quinoa cooked'], s: '150g cooked', kcal: 180, p: 6.6, c: 32, f: 2.8, fiber: 4.2, sugar: 1.2, sodium: 8, vitC: 0, ca: 24, fe: 2.1 },
  { n: 'lentils', a: ['lentil', 'lentils cooked'], s: '150g cooked', kcal: 173, p: 13, c: 30, f: 0.6, fiber: 11, sugar: 2.7, sodium: 3, vitC: 2, ca: 27, fe: 4.7 },
  { n: 'chickpeas', a: ['chickpea', 'garbanzo'], s: '150g cooked', kcal: 195, p: 11, c: 32, f: 3, fiber: 9, sugar: 5.5, sodium: 7, vitC: 2, ca: 42, fe: 3.4 },
  { n: 'tuna', a: ['tuna canned', 'tuna in water'], s: '100g canned', kcal: 116, p: 26, c: 0, f: 1, fiber: 0, sugar: 0, sodium: 247, vitC: 0, ca: 13, fe: 1.1 },
  { n: 'shrimp', a: ['prawns'], s: '100g cooked', kcal: 99, p: 24, c: 0.2, f: 0.3, fiber: 0, sugar: 0, sodium: 111, vitC: 0, ca: 70, fe: 0.3 },
  { n: 'olive oil', a: ['olive oil 1tbsp'], s: '14g', kcal: 119, p: 0, c: 0, f: 13.5, fiber: 0, sugar: 0, sodium: 0, vitC: 0, ca: 0, fe: 0.1 },
  { n: 'carrot', a: ['carrots'], s: '1 medium', kcal: 25, p: 0.6, c: 6, f: 0.1, fiber: 1.7, sugar: 2.9, sodium: 42, vitC: 3.6, ca: 20, fe: 0.2 },
  { n: 'tomato', a: ['tomatoes'], s: '1 medium', kcal: 22, p: 1.1, c: 4.8, f: 0.2, fiber: 1.5, sugar: 3.2, sodium: 6, vitC: 16, ca: 12, fe: 0.3 },
  { n: 'cucumber', a: ['cucumbers'], s: '100g', kcal: 15, p: 0.7, c: 3.6, f: 0.1, fiber: 0.5, sugar: 1.7, sodium: 2, vitC: 2.8, ca: 16, fe: 0.3 },
  { n: 'cabbage', a: ['cabbage raw'], s: '100g', kcal: 25, p: 1.3, c: 5.8, f: 0.1, fiber: 2.5, sugar: 3.2, sodium: 18, vitC: 37, ca: 40, fe: 0.5 },
  { n: 'potato', a: ['potato baked', 'white potato'], s: '150g', kcal: 161, p: 4.3, c: 37, f: 0.2, fiber: 3.4, sugar: 2.2, sodium: 17, vitC: 17, ca: 20, fe: 1.4 },
  { n: 'sweet corn', a: ['corn', 'corn on cob'], s: '1 ear', kcal: 88, p: 3.3, c: 19, f: 1.4, fiber: 2.4, sugar: 4.5, sodium: 3, vitC: 7, ca: 3, fe: 0.5 },
  { n: 'black beans', a: ['black beans cooked'], s: '150g cooked', kcal: 165, p: 10, c: 30, f: 0.7, fiber: 12, sugar: 0.5, sodium: 1.5, vitC: 0, ca: 43, fe: 3 },
  { n: 'walnuts', a: ['walnut'], s: '28g', kcal: 185, p: 4.3, c: 3.9, f: 18.5, fiber: 1.9, sugar: 0.7, sodium: 1, vitC: 0, ca: 28, fe: 0.7 },
  { n: 'dark chocolate', a: ['dark chocolate 70%'], s: '20g', kcal: 120, p: 1.6, c: 9, f: 8.4, fiber: 1.6, sugar: 5.4, sodium: 4, vitC: 0, ca: 10, fe: 1.2 },
  { n: 'green tea', a: ['green tea cup'], s: '250ml', kcal: 2, p: 0, c: 0.5, f: 0, fiber: 0, sugar: 0, sodium: 0, vitC: 0, ca: 0, fe: 0 },
  { n: 'coffee', a: ['black coffee', 'coffee cup'], s: '250ml', kcal: 2, p: 0.3, c: 0, f: 0, fiber: 0, sugar: 0, sodium: 5, vitC: 0, ca: 5, fe: 0 },
  { n: 'honey', a: ['honey 1tbsp'], s: '21g', kcal: 64, p: 0.1, c: 17, f: 0, fiber: 0, sugar: 17, sodium: 1, vitC: 0, ca: 1, fe: 0.1 },
  { n: 'egg white', a: ['egg whites'], s: '3 whites', kcal: 51, p: 11, c: 0.7, f: 0.2, fiber: 0, sugar: 0.7, sodium: 166, vitC: 0, ca: 6, fe: 0.1 },
  { n: 'whey protein', a: ['whey', 'protein powder'], s: '30g scoop', kcal: 120, p: 24, c: 3, f: 1.5, fiber: 0.5, sugar: 2, sodium: 130, vitC: 0, ca: 150, fe: 0.3 },
  { n: 'kale', a: ['kale raw'], s: '100g', kcal: 49, p: 4.3, c: 8.8, f: 0.9, fiber: 3.6, sugar: 2.3, sodium: 29, vitC: 120, ca: 150, fe: 1.6 },
  { n: 'mushroom', a: ['mushrooms'], s: '100g', kcal: 22, p: 3.1, c: 3.3, f: 0.3, fiber: 1, sugar: 2, sodium: 5, vitC: 2.1, ca: 3, fe: 0.5 },
  { n: 'onion', a: ['onions'], s: '100g', kcal: 40, p: 1.1, c: 9.3, f: 0.1, fiber: 1.7, sugar: 4.2, sodium: 4, vitC: 7.4, ca: 23, fe: 0.2 },
  { n: 'garlic', a: ['garlic clove'], s: '3g', kcal: 4, p: 0.2, c: 1, f: 0, fiber: 0.1, sugar: 0, sodium: 1, vitC: 0.9, ca: 5, fe: 0.1 }
];

const FOOD_INDEX = (function () {
  const map = {};
  FOODS.forEach(function (f, i) {
    map[f.n] = i;
    (f.a || []).forEach(function (al) { if (!(al in map)) map[al] = i; });
  });
  return map;
})();

function findFood(name) {
  const key = String(name).toLowerCase().trim();
  const idx = FOOD_INDEX[key];
  return idx === undefined ? null : FOODS[idx];
}

/* ------------------------------------------------------------------ */
/* Nutrition analyzer                                                 */
/* ------------------------------------------------------------------ */
/* Meal spec format: "breakfast=oatmeal:50,banana,milk:250;lunch=..." */
function parseMealSpec(spec) {
  const meals = [];
  String(spec || '').split(';').forEach(function (part) {
    part = part.trim();
    if (!part) return;
    let name = 'Meal ' + (meals.length + 1);
    let body = part;
    const eq = part.indexOf('=');
    if (eq !== -1) { name = part.slice(0, eq).trim(); body = part.slice(eq + 1); }
    const items = [];
    String(body).split(',').forEach(function (item) {
      item = item.trim();
      if (!item) return;
      let foodName = item; let grams = null;
      const colon = item.lastIndexOf(':');
      if (colon !== -1) {
        const g = Number(item.slice(colon + 1));
        if (!Number.isNaN(g) && g > 0) { foodName = item.slice(0, colon).trim(); grams = g; }
      }
      items.push({ food: foodName, grams: grams });
    });
    if (items.length) meals.push({ name: name, items: items });
  });
  return meals;
}

function lookupFoodItem(item) {
  const food = findFood(item.food);
  if (!food) return { ok: false, name: item.food, grams: item.grams };
  return { ok: true, food: food, grams: item.grams };
}

function servingGrams(food) {
  if (food.s.indexOf('g') === -1) return 100;
  const m = food.s.match(/(\d+(?:\.\d+)?)g/);
  return m ? Number(m[1]) : 100;
}

function scaleNutrient(food, grams) {
  const base = servingGrams(food);
  const k = grams / base;
  return {
    grams: grams,
    kcal: food.kcal * k, p: food.p * k, c: food.c * k, f: food.f * k,
    fiber: food.fiber * k, sugar: food.sugar * k, sodium: food.sodium * k,
    vitC: food.vitC * k, ca: food.ca * k, fe: food.fe * k
  };
}

function analyzeMeal(spec) {
  const meals = parseMealSpec(spec);
  const result = { meals: [], totals: { kcal: 0, p: 0, c: 0, f: 0, fiber: 0, sugar: 0, sodium: 0, vitC: 0, ca: 0, fe: 0 }, unknown: [] };
  if (!meals.length) return result;
  meals.forEach(function (meal) {
    const m = { name: meal.name, items: [], totals: { kcal: 0, p: 0, c: 0, f: 0, fiber: 0, sugar: 0, sodium: 0, vitC: 0, ca: 0, fe: 0 } };
    meal.items.forEach(function (item) {
      const lu = lookupFoodItem(item);
      if (!lu.ok) { result.unknown.push(lu.name); return; }
      const grams = lu.grams || servingGrams(lu.food);
      const scaled = scaleNutrient(lu.food, grams);
      m.items.push({ name: lu.food.n, grams: round0(grams), kcal: round1(scaled.kcal), p: round1(scaled.p), c: round1(scaled.c), f: round1(scaled.f), fiber: round1(scaled.fiber) });
      ['kcal', 'p', 'c', 'f', 'fiber', 'sugar', 'sodium', 'vitC', 'ca', 'fe'].forEach(function (k) {
        m.totals[k] += scaled[k];
      });
    });
    result.meals.push(m);
    ['kcal', 'p', 'c', 'f', 'fiber', 'sugar', 'sodium', 'vitC', 'ca', 'fe'].forEach(function (k) {
      result.totals[k] += m.totals[k];
    });
  });
  return result;
}

function rdaLabel(key) {
  const labels = {
    kcal: 'Calories (kcal)', p: 'Protein (g)', c: 'Carbs (g)', f: 'Fat (g)',
    fiber: 'Fiber (g)', sugar: 'Sugar (g)', sodium: 'Sodium (mg)',
    vitC: 'Vitamin C (mg)', ca: 'Calcium (mg)', fe: 'Iron (mg)'
  };
  return labels[key] || key;
}

function renderNutrition(result, opts) {
  const lines = [];
  lines.push('Nutrition analysis');
  lines.push(hr());
  if (!result.meals.length) {
    lines.push('No valid foods parsed. Example: --meal "breakfast=oatmeal:50,banana,milk:250"');
    return lines.join('\n');
  }
  result.meals.forEach(function (m) {
    lines.push(m.name + ':');
    if (!m.items.length) { lines.push('  (no recognized foods)'); return; }
    m.items.forEach(function (it) {
      lines.push('  ' + padR(it.name, 24) + ' ' + it.grams + 'g  ' + it.kcal + ' kcal  P' + it.p + ' C' + it.c + ' F' + it.f);
    });
    lines.push('  ' + padR('subtotal', 24) + ' ' + round0(m.totals.kcal) + ' kcal  P' + round0(m.totals.p) + 'g  C' + round0(m.totals.c) + 'g  F' + round0(m.totals.f) + 'g');
  });
  lines.push(hr());
  lines.push(padR('Total', 24) + ' ' + round0(result.totals.kcal) + ' kcal');
  const keys = ['protein', 'carbs', 'fat', 'fiber', 'sugar', 'sodium', 'vitC', 'calcium', 'iron'];
  const totalsMap = { protein: 'p', carbs: 'c', fat: 'f', fiber: 'fiber', sugar: 'sugar', sodium: 'sodium', vitC: 'vitC', calcium: 'ca', iron: 'fe' };
  keys.forEach(function (key) {
    const tk = totalsMap[key];
    const val = result.totals[tk];
    const target = RDA[key];
    const ratio = pct(val, target);
    const shortKey = { protein: 'p', carbs: 'c', fat: 'f', fiber: 'fiber', sugar: 'sugar', sodium: 'sodium', vitC: 'vitC', calcium: 'ca', iron: 'fe' }[key];
    lines.push('  ' + padR(rdaLabel(shortKey), 24) + ' ' + pad(round0(val), 6) + ' / ' + target + '  [' + bar(ratio, 130, 14) + '] ' + ratio + '% of RDA');
  });
  if (result.unknown.length) {
    lines.push(hr());
    lines.push('Unrecognized foods (check spelling): ' + result.unknown.join(', '));
  }
  lines.push('Protein share of calories: ' + pct(result.totals.p * 4, result.totals.kcal) + '% (target 10-35%)');
  lines.push('Fat share of calories:     ' + pct(result.totals.f * 9, result.totals.kcal) + '% (target 20-35%)');
  return lines.join('\n');
}

/* ------------------------------------------------------------------ */
/* BMR / TDEE calculator                                              */
/* ------------------------------------------------------------------ */
function bmrMifflin(sex, age, heightCm, weightKg) {
  // Mifflin-St Jeor equation
  const base = 10 * weightKg + 6.25 * heightCm - 5 * age;
  return sex === 'male' ? base + 5 : base - 161;
}

function bmrHarrisBenedict(sex, age, heightCm, weightKg) {
  // Revised Harris-Benedict equation
  if (sex === 'male') {
    return 88.362 + (13.397 * weightKg) + (4.799 * heightCm) - (5.677 * age);
  }
  return 447.593 + (9.247 * weightKg) + (3.098 * heightCm) - (4.330 * age);
}

function computeBMR(sex, age, heightCm, weightKg) {
  const m = bmrMifflin(sex, age, heightCm, weightKg);
  const hb = bmrHarrisBenedict(sex, age, heightCm, weightKg);
  return { mifflin: m, harrisBenedict: hb, recommended: Math.round((m + hb) / 2) };
}

function computeTDEE(bmr, activityKey) {
  const factor = ACTIVITY_LEVELS[activityKey].factor;
  return { tdee: Math.round(bmr * factor), factor: factor, activityKey: activityKey };
}

function goalTargets(tdee, goal) {
  if (goal === 'lose') return { label: 'Mild fat loss (-20%)', kcal: Math.round(tdee * 0.8), proteinBoost: 1.6 };
  if (goal === 'gain') return { label: 'Lean muscle gain (+12%)', kcal: Math.round(tdee * 1.12), proteinBoost: 1.8 };
  return { label: 'Maintenance', kcal: tdee, proteinBoost: 1.4 };
}

function bmiCategory(bmi) {
  if (bmi < 18.5) return 'underweight';
  if (bmi < 25) return 'normal weight';
  if (bmi < 30) return 'overweight';
  return 'obese';
}

function renderBMR(sex, age, heightCm, weightKg, activityKey, goal) {
  const bmr = computeBMR(sex, age, heightCm, weightKg);
  const tdeeObj = computeTDEE(bmr.recommended, activityKey);
  const bmi = weightKg / Math.pow(heightCm / 100, 2);
  const target = goalTargets(tdeeObj.tdee, goal || 'maintain');
  const proteinG = Math.round(weightKg * target.proteinBoost);
  const lines = [];
  lines.push('BMR / TDEE calculator');
  lines.push(hr());
  lines.push('Profile      : ' + (sex === 'male' ? 'male' : 'female') + ', ' + age + 'y, ' + heightCm + 'cm, ' + weightKg + 'kg');
  lines.push('BMI          : ' + round1(bmi) + '  ' + bmiCategory(bmi));
  lines.push('BMR (Mifflin-St Jeor)        : ' + round0(bmr.mifflin) + ' kcal/day');
  lines.push('BMR (Harris-Benedict)        : ' + round0(bmr.harrisBenedict) + ' kcal/day');
  lines.push('BMR (average)                : ' + round0(bmr.recommended) + ' kcal/day');
  lines.push('Activity (' + ACTIVITY_LEVELS[activityKey].label + '): x' + tdeeObj.factor);
  lines.push('TDEE (maintenance)           : ' + tdeeObj.tdee + ' kcal/day');
  lines.push(hr());
  lines.push('Goal (' + target.label + '): ' + target.kcal + ' kcal/day');
  lines.push('Suggested protein intake     : ' + proteinG + ' g/day (' + target.proteinBoost + ' g per kg bodyweight)');
  lines.push('Suggested carbs (50%)        : ' + Math.round(target.kcal * 0.5 / 4) + ' g/day');
  lines.push('Suggested fat (25%)          : ' + Math.round(target.kcal * 0.25 / 9) + ' g/day');
  lines.push('Rate of change: ' + (goal === 'lose' ? '~0.4-0.6 kg/week, sustainable' : goal === 'gain' ? '~0.2-0.4 kg/week, lean' : 'weight stable'));
  if (bmi < 18.5 || bmi > 27) {
    lines.push(hr());
    lines.push('NOTE: Your BMI (' + round1(bmi) + ') is outside the 18.5-27 range.');
    lines.push('Please consider consulting a healthcare professional before starting any diet.');
  }
  return lines.join('\n');
}

/* ------------------------------------------------------------------ */
/* Workout plan generator                                             */
/* ------------------------------------------------------------------ */
const WORKOUT_LIBRARY = {
  bodyweight: {
    push: ['Push-ups', 'Incline push-ups', 'Diamond push-ups', 'Pike push-ups'],
    pull: ['Bodyweight rows', 'Chin-up negatives', 'Doorway rows', 'Superman holds'],
    legs: ['Bodyweight squats', 'Lunges', 'Bulgarian split squats', 'Glute bridges', 'Calf raises'],
    core: ['Plank', 'Side plank', 'Dead bug', 'Leg raises', 'Russian twists']
  },
  gym: {
    push: ['Bench press', 'Overhead press', 'Incline dumbbell press', 'Cable fly'],
    pull: ['Lat pulldown', 'Barbell row', 'Seated cable row', 'Face pulls'],
    legs: ['Back squat', 'Romanian deadlift', 'Leg press', 'Walking lunges', 'Calf raises'],
    core: ['Plank', 'Hanging leg raises', 'Cable crunch', 'Pallof press']
  },
  mixed: {
    push: ['Push-ups', 'Dumbbell bench press', 'Overhead press', 'Dips'],
    pull: ['Bodyweight rows', 'Dumbbell rows', 'Lat pulldown', 'Reverse fly'],
    legs: ['Goblet squats', 'Walking lunges', 'Romanian deadlift', 'Step-ups'],
    core: ['Plank', 'Hollow hold', 'Leg raises', 'Side plank']
  }
};

function generatePlan(goal, days, equipment, level) {
  const lib = WORKOUT_LIBRARY[equipment];
  const repScheme = goal === 'strength' ? { sets: 5, reps: '3-5', rest: '3 min', note: 'heavy, low reps' }
    : goal === 'muscle' ? { sets: 4, reps: '8-12', rest: '90 s', note: 'moderate, hypertrophy' }
    : { sets: 3, reps: '12-20', rest: '60 s', note: 'light-moderate, endurance' };
  const dayNames = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
  const patterns = {
    2: ['A', 'B'],
    3: ['A', 'B', 'C'],
    4: ['A', 'B', 'A', 'C'],
    5: ['A', 'B', 'C', 'A', 'B'],
    6: ['A', 'B', 'A', 'C', 'A', 'B']
  };
  const focusMap = { A: ['push', 'core'], B: ['pull', 'legs'], C: ['legs', 'core'] };
  const plan = [];
  const used = {};
  const pattern = patterns[days] || patterns[3];
  for (let i = 0; i < days; i++) {
    const focus = focusMap[pattern[i]];
    const exercises = [];
    focus.forEach(function (group) {
      const pool = lib[group];
      const pick = pool[(used[group] = (used[group] || 0) + 1) % pool.length];
      exercises.push({ group: group, name: pick });
    });
    plan.push({ day: dayNames[i], focus: focus.join(' + '), exercises: exercises, scheme: repScheme });
  }
  const warmup = level === 'beginner' ? ['5 min light cardio', 'dynamic stretches'] : ['8 min light cardio', 'mobility drills', 'activation sets'];
  const cool = ['5 min walk', 'static stretching'];
  return { plan: plan, warmup: warmup, cool: cool, repScheme: repScheme, goal: goal, days: days, equipment: equipment, level: level };
}

function renderPlan(g) {
  const lines = [];
  lines.push('Workout plan — goal: ' + g.goal + ', ' + g.days + ' days/week, equipment: ' + g.equipment + ', level: ' + g.level);
  lines.push(hr());
  lines.push('Warm-up: ' + g.warmup.join(' + '));
  lines.push('Sets x reps: ' + g.repScheme.sets + ' x ' + g.repScheme.reps + '  (rest ' + g.repScheme.rest + ') — ' + g.repScheme.note);
  lines.push(hr());
  g.plan.forEach(function (d) {
    lines.push(d.day + ' — ' + d.focus + ':');
    d.exercises.forEach(function (e) {
      lines.push('  - ' + e.name + '  ' + g.repScheme.sets + ' x ' + g.repScheme.reps);
    });
  });
  lines.push(hr());
  lines.push('Cool-down: ' + g.cool.join(' + '));
  lines.push('Progression: add ' + (g.level === 'beginner' ? '1-2 reps per week, then add a set' : '2.5-5% load or 1-2 reps weekly') + ' when you hit the top of the rep range on all sets.');
  lines.push('Rest day guidance: at least ' + (g.days >= 5 ? '1-2 full rest days' : '1 full rest day') + ' per week; sleep 7-9h for recovery.');
  return lines.join('\n');
}

/* ------------------------------------------------------------------ */
/* Guided breathing                                                   */
/* ------------------------------------------------------------------ */
const BREATH_TECHNIQUES = {
  '478': { name: '4-7-8 Relaxing breath', inhale: 4, hold: 7, exhale: 8, guide: ['Inhale quietly through the nose for 4 counts.', 'Hold the breath gently for 7 counts.', 'Exhale fully through the mouth for 8 counts.'] },
  box: { name: 'Box breathing', inhale: 4, hold: 4, exhale: 4, hold2: 4, guide: ['Inhale for 4 counts.', 'Hold for 4 counts.', 'Exhale for 4 counts.', 'Hold empty for 4 counts.'] },
  coherent: { name: 'Coherent breathing (5-5)', inhale: 5, hold: 0, exhale: 5, guide: ['Inhale for 5 counts.', 'Exhale for 5 counts.', 'Keep the breath smooth and even.'] }
};

function breatheSession(techniqueKey, rounds, nowait) {
  const t = BREATH_TECHNIQUES[techniqueKey];
  const roundSec = t.inhale + (t.hold || 0) + t.exhale + (t.hold2 || 0);
  const totalSec = roundSec * rounds;
  const lines = [];
  lines.push(t.name + ' — ' + rounds + ' round(s), ~' + totalSec + 's total');
  lines.push(hr());
  t.guide.forEach(function (g) { lines.push('  ' + g); });
  lines.push(hr());
  for (let r = 1; r <= rounds; r++) {
    const seq = [];
    seq.push('inhale ' + t.inhale + 's');
    if (t.hold) seq.push('hold ' + t.hold + 's');
    seq.push('exhale ' + t.exhale + 's');
    if (t.hold2) seq.push('hold ' + t.hold2 + 's');
    lines.push('Round ' + r + ': ' + seq.join(' -> '));
  }
  lines.push(hr());
  if (!nowait) {
    lines.push('(live mode: breathing now — press Ctrl+C to stop early)');
  } else {
    lines.push('(nowait mode: schedule only, no real-time pacing)');
  }
  lines.push('Tip: practice 1-2 sessions daily; stop if you feel dizzy and return to normal breathing.');
  return { lines: lines.join('\n'), totalSec: totalSec, technique: t.name };
}

function liveBreathe(techniqueKey, rounds, onStep) {
  const t = BREATH_TECHNIQUES[techniqueKey];
  const phases = [];
  phases.push({ label: 'Inhale', sec: t.inhale });
  if (t.hold) phases.push({ label: 'Hold', sec: t.hold });
  phases.push({ label: 'Exhale', sec: t.exhale });
  if (t.hold2) phases.push({ label: 'Hold', sec: t.hold2 });
  for (let r = 1; r <= rounds; r++) {
    phases.forEach(function (ph) { onStep(r, ph.label, ph.sec); });
  }
}

/* ------------------------------------------------------------------ */
/* Sleep quality analysis                                             */
/* ------------------------------------------------------------------ */
function parseClock(s) {
  const m = String(s || '').match(/^(\d{1,2}):(\d{2})$/);
  if (!m) return null;
  const h = Number(m[1]); const min = Number(m[2]);
  if (h < 0 || h > 23 || min < 0 || min > 59) return null;
  return h * 60 + min;
}

function minutesBetween(bedMin, riseMin) {
  if (riseMin > bedMin) return riseMin - bedMin;
  return (riseMin + 24 * 60) - bedMin; // crossed midnight
}

function sleepScore(efficiency, durationMin, wakeups, latencyMin) {
  let score = 100;
  if (efficiency < 85) score -= (85 - efficiency) * 1.5;
  if (durationMin < 420) score -= (420 - durationMin) * 0.15;  // under 7h
  if (durationMin > 600) score -= (durationMin - 600) * 0.05;  // over 10h
  score -= Math.min(20, (wakeups - 1) * 4);
  if (latencyMin > 20) score -= Math.min(15, (latencyMin - 20) * 0.5);
  return clamp(Math.round(score), 0, 100);
}

function analyzeSleep(bedStr, riseStr, latencyMin, wakeups, awakeMin) {
  const bed = parseClock(bedStr);
  const rise = parseClock(riseStr);
  if (bed === null || rise === null) return { ok: false, msg: 'bed and rise must use HH:MM 24h format' };
  const inBed = minutesBetween(bed, rise);
  const awake = latencyMin + awakeMin + wakeups * 1; // approx: 1 min per wake-up transition
  const asleep = Math.max(0, inBed - awake);
  const efficiency = inBed > 0 ? Math.round((asleep / inBed) * 100) : 0;
  const score = sleepScore(efficiency, asleep, wakeups, latencyMin);
  const h = Math.floor(asleep / 60); const mm = asleep % 60;
  return {
    ok: true,
    inBedMin: inBed, asleepMin: asleep, awakeMin: awake,
    efficiency: efficiency, score: score,
    durationText: h + 'h ' + mm + 'm',
    grade: score >= 85 ? 'Excellent' : score >= 70 ? 'Good' : score >= 50 ? 'Fair' : 'Poor'
  };
}

function renderSleep(r) {
  if (!r.ok) return 'Sleep analysis: ' + r.msg;
  const lines = [];
  lines.push('Sleep quality analysis');
  lines.push(hr());
  lines.push('Time in bed     : ' + Math.floor(r.inBedMin / 60) + 'h ' + (r.inBedMin % 60) + 'm');
  lines.push('Estimated asleep: ' + r.durationText + '  (awake ~' + r.awakeMin + ' min)');
  lines.push('Sleep efficiency: ' + r.efficiency + '%  (target >= 85%)');
  lines.push('Sleep score     : ' + r.score + '/100  (' + r.grade + ')');
  lines.push(hr());
  const tips = [];
  if (r.efficiency < 85) tips.push('Work on sleep efficiency: keep the bed for sleeping only, avoid screens 30-60 min before bed.');
  if (r.asleepMin < 420) tips.push('Duration is below 7h: aim for 7-9h; try a consistent bedtime even on weekends.');
  if (r.asleepMin > 600) tips.push('Duration above 10h: very long sleep can indicate other issues; consider a sleep check-up.');
  if (r.wakeups > 1) tips.push('Frequent night waking: limit caffeine after 2pm and alcohol before bed.');
  if (!tips.length) tips.push('Your sleep metrics look healthy. Keep the consistent schedule and dark, cool bedroom.');
  tips.forEach(function (t) { lines.push('  - ' + t); });
  lines.push('NOTE: persistent insomnia or fatigue despite 7-9h sleep warrants a medical consultation.');
  return lines.join('\n');
}

/* ------------------------------------------------------------------ */
/* Trend dashboard (CSV)                                              */
/* ------------------------------------------------------------------ */
/*
 * CSV format: header row, one row per day.
 *   date,weight,steps,sleep_hours,water_l,energy,stress,mood
 * Example:
 *   2026-07-01,82.4,7200,7.2,2.1,7,4,7
 *   2026-07-02,82.1,8600,7.6,2.4,8,3,8
 */
function parseTrendCSV(csvText) {
  const rows = [];
  const lines = String(csvText || '').split(/\r?\n/).map(function (l) { return l.trim(); }).filter(Boolean);
  if (!lines.length) return rows;
  const header = lines[0].split(',').map(function (h) { return h.trim().toLowerCase(); });
  for (let i = 1; i < lines.length; i++) {
    const cells = lines[i].split(',');
    if (cells.length < header.length) continue;
    const row = {};
    header.forEach(function (h, idx) { row[h] = cells[idx].trim(); });
    rows.push(row);
  }
  return { header: header, rows: rows };
}

function num(v) { const n = Number(v); return Number.isNaN(n) ? null : n; }

function analyzeTrend(csvText) {
  const data = parseTrendCSV(csvText);
  const result = { series: {}, notes: [], count: 0 };
  if (!data.rows || !data.rows.length) return result;
  result.count = data.rows.length;
  ['weight', 'steps', 'sleep_hours', 'water_l', 'energy', 'stress', 'mood'].forEach(function (key) {
    const vals = data.rows.map(function (r) { return num(r[key]); }).filter(function (v) { return v !== null; });
    if (!vals.length) return;
    const sum = vals.reduce(function (a, b) { return a + b; }, 0);
    const avg = sum / vals.length;
    const first = vals[0]; const last = vals[vals.length - 1];
    const min = Math.min.apply(null, vals); const max = Math.max.apply(null, vals);
    const trend = vals.length >= 2 ? (last - first) : 0;
    result.series[key] = { avg: avg, min: min, max: max, first: first, last: last, trend: trend, n: vals.length };
  });
  if (result.series.weight) {
    const w = result.series.weight;
    if (w.trend < -0.5) result.notes.push('Weight trending down (' + round1(w.trend) + ' kg over ' + w.n + ' days) - steady progress.');
    else if (w.trend > 0.5) result.notes.push('Weight trending up (' + round1(w.trend) + ' kg) - review intake if unintended.');
    else result.notes.push('Weight stable (' + round1(w.trend) + ' kg) - maintenance phase.');
  }
  if (result.series.sleep_hours) {
    const s = result.series.sleep_hours;
    if (s.avg < 7) result.notes.push('Average sleep ' + round1(s.avg) + 'h - below the 7h target, prioritize bedtime.');
    else result.notes.push('Average sleep ' + round1(s.avg) + 'h - within the healthy 7-9h range.');
  }
  if (result.series.steps) {
    const st = result.series.steps;
    if (st.avg < 6000) result.notes.push('Average steps ' + round0(st.avg) + '/day - try to reach 7-10k.');
    else result.notes.push('Average steps ' + round0(st.avg) + '/day - good activity level.');
  }
  if (result.series.mood && result.series.stress) {
    const m = result.series.mood; const s = result.series.stress;
    result.notes.push('Mood avg ' + round1(m.avg) + '/10, stress avg ' + round1(s.avg) + '/10 - ' + (s.avg > 6 ? 'consider stress management (see breathe tool).' : 'stress levels look manageable.'));
  }
  return result;
}

function renderTrend(csvText) {
  const r = analyzeTrend(csvText);
  const lines = [];
  lines.push('Health trend dashboard');
  lines.push(hr());
  if (!r.count) {
    lines.push('No data rows found. Provide CSV with header: date,weight,steps,sleep_hours,water_l,energy,stress,mood');
    return lines.join('\n');
  }
  lines.push('Records: ' + r.count + ' day(s)');
  lines.push(hr());
  const labels = { weight: 'Weight (kg)', steps: 'Steps', sleep_hours: 'Sleep (h)', water_l: 'Water (L)', energy: 'Energy (1-10)', stress: 'Stress (1-10)', mood: 'Mood (1-10)' };
  Object.keys(labels).forEach(function (key) {
    const s = r.series[key];
    if (!s) return;
    lines.push('  ' + padR(labels[key], 16) + ' avg ' + pad(round1(s.avg), 7) + '  min ' + pad(round1(s.min), 7) + '  max ' + pad(round1(s.max), 7) + '  trend ' + (s.trend >= 0 ? '+' : '') + round1(s.trend));
  });
  if (r.series.weight && r.series.weight.n >= 2) {
    lines.push(hr());
    lines.push('Weight trend (last value: ' + r.series.weight.last + ' kg):');
    const w = r.series.weight;
    const span = Math.max(0.1, w.max - w.min);
    lines.push('  ' + bar(100 - ((w.last - w.min) / span) * 100, 100, 40) + '');
    lines.push('  ' + padR('min ' + w.min + ' kg', 20) + ' ' + padR('current ' + w.last + ' kg', 20) + ' max ' + w.max + ' kg');
  }
  if (r.notes.length) {
    lines.push(hr());
    lines.push('Insights:');
    r.notes.forEach(function (n) { lines.push('  - ' + n); });
  }
  return lines.join('\n');
}

/* ------------------------------------------------------------------ */
/* Demo data + integrated report                                      */
/* ------------------------------------------------------------------ */
const DEMO_MEAL = 'breakfast=oatmeal:50,banana,milk:250;lunch=chicken breast:120,white rice:150,broccoli:100;dinner=salmon:120,sweet potato:150,spinach:60';
const DEMO_SLEEP = { bed: '23:30', rise: '07:00', latency: 15, wakeups: 1, awake: 10 };
const DEMO_CSV = [
  'date,weight,steps,sleep_hours,water_l,energy,stress,mood',
  '2026-07-27,83.1,5400,6.4,1.6,5,7,5',
  '2026-07-28,83.0,6800,6.8,1.8,6,6,6',
  '2026-07-29,82.7,7900,7.1,2.0,6,6,6',
  '2026-07-30,82.5,8200,7.3,2.1,7,5,7',
  '2026-07-31,82.4,8600,7.6,2.3,7,4,7',
  '2026-08-01,82.2,9100,7.8,2.4,8,4,8',
  '2026-08-02,82.1,8800,7.5,2.3,7,5,7'
].join('\n');

function renderReport(opts) {
  const o = opts || {};
  const sex = o.sex || 'male';
  const age = o.age || 30;
  const height = o.height || 175;
  const weight = o.weight || 82;
  const activity = o.activity || 'light';
  const goal = o.goal || 'maintain';
  const meal = o.meal || DEMO_MEAL;
  const sleep = o.sleep || DEMO_SLEEP;
  const csv = o.csv || DEMO_CSV;
  const lines = [];
  lines.push('=== VitaFlow Integrated Wellness Report ===');
  lines.push('');
  lines.push(renderBMR(sex, age, height, weight, activity, goal));
  lines.push('');
  lines.push(renderNutrition(analyzeMeal(meal)));
  lines.push('');
  lines.push(renderSleep(analyzeSleep(sleep.bed, sleep.rise, sleep.latency, sleep.wakeups, sleep.awake)));
  lines.push('');
  lines.push(renderPlan(generatePlan(goal === 'lose' ? 'endurance' : goal === 'gain' ? 'muscle' : 'strength', 3, 'bodyweight', 'beginner')));
  lines.push('');
  lines.push(renderTrend(csv));
  lines.push('');
  lines.push(hr());
  lines.push(DISCLAIMER);
  return lines.join('\n');
}

/* ------------------------------------------------------------------ */
/* Self-test suite                                                    */
/* ------------------------------------------------------------------ */
function runSelfTest() {
  let pass = 0; let fail = 0;
  const failures = [];
  function check(name, cond, extra) {
    if (cond) { pass++; } else { fail++; failures.push(name + (extra ? ' :: ' + extra : '')); }
  }
  function close(a, b, tol) { return Math.abs(a - b) <= (tol || 0.001); }

  // nutrition
  const meal = analyzeMeal(DEMO_MEAL);
  check('nutrition has 3 meals', meal.meals.length === 3);
  check('nutrition totals kcal > 0', meal.totals.kcal > 0);
  const banana = scaleNutrient(findFood('banana'), 105);
  check('banana scaling 1 serving', close(banana.kcal, 105, 1), 'kcal=' + banana.kcal);
  const halfBanana = scaleNutrient(findFood('banana'), 52.5);
  check('banana half scaling', close(halfBanana.kcal, 52.5, 1), 'kcal=' + halfBanana.kcal);
  check('findFood alias', findFood('oats') !== null && findFood('OATS') !== null);
  check('findFood unknown returns null', findFood('pizza') === null);

  // bmr
  const bmr = computeBMR('male', 30, 175, 82);
  check('BMR male positive', bmr.recommended > 1400 && bmr.recommended < 2200, 'bmr=' + bmr.recommended);
  const bmrF = computeBMR('female', 30, 165, 60);
  check('BMR female lower than male', bmrF.recommended < bmr.recommended);
  const tdee = computeTDEE(bmr.recommended, 'moderate');
  check('TDEE > BMR', tdee.tdee > bmr.recommended);
  const lose = goalTargets(tdee.tdee, 'lose');
  check('lose goal -20%', close(lose.kcal, tdee.tdee * 0.8, 1));

  // workout plan
  const plan = generatePlan('strength', 3, 'bodyweight', 'beginner');
  check('plan has 3 days', plan.plan.length === 3);
  check('plan days have exercises', plan.plan.every(function (d) { return d.exercises.length >= 2; }));
  const plan5 = generatePlan('muscle', 5, 'gym', 'intermediate');
  check('plan 5 days', plan5.plan.length === 5);

  // breathing
  const b = breatheSession('478', 2, true);
  check('breath 4-7-8 rounds', b.lines.indexOf('Round 1') !== -1 && b.lines.indexOf('Round 2') !== -1);
  check('breath total 38s for 2 rounds', b.totalSec === 38, 'total=' + b.totalSec);
  const box = breatheSession('box', 1, true);
  check('box total 16s', box.totalSec === 16, 'total=' + box.totalSec);

  // sleep
  const s = analyzeSleep('23:30', '07:00', 15, 1, 10);
  check('sleep in bed 450min', s.inBedMin === 450, 'inbed=' + s.inBedMin);
  check('sleep score 0-100', s.score >= 0 && s.score <= 100);
  check('sleep efficiency computed', s.efficiency >= 80, 'eff=' + s.efficiency);
  const bad = analyzeSleep('01:00', '05:00', 40, 4, 30);
  check('sleep bad score lower', bad.score < s.score, 'bad=' + bad.score + ' good=' + s.score);
  check('sleep invalid time rejected', analyzeSleep('25:00', '07:00', 5, 1, 5).ok === false);
  check('sleep crossed midnight', analyzeSleep('23:00', '06:30', 10, 0, 5).inBedMin === 450);

  // trend
  const trend = analyzeTrend(DEMO_CSV);
  check('trend 7 records', trend.count === 7);
  check('trend weight series', trend.series.weight !== undefined && trend.series.weight.n === 7);
  check('trend weight down', trend.series.weight.trend < 0);
  check('trend insights non-empty', trend.notes.length > 0);

  // validation / safety
  check('validation age low rejected', checkNumber('age', 10, 18, 120, 'years').ok === false);
  check('validation age ok', checkNumber('age', 30, 18, 120, 'years').ok === true);
  check('validation sex bad', checkSex('x').ok === false);
  check('validation sex ok', checkSex('M').ok === true);
  check('validation activity bad', checkActivity('extreme').ok === false);
  check('validation activity ok', checkActivity('active').ok === true);

  // disclaimer
  check('disclaimer mentions medical', DISCLAIMER.toLowerCase().indexOf('medical') !== -1);
  check('disclaimer mentions physician', DISCLAIMER.toLowerCase().indexOf('physician') !== -1);

  return { pass: pass, fail: fail, failures: failures };
}

/* ------------------------------------------------------------------ */
/* CLI                                                                */
/* ------------------------------------------------------------------ */
function usage() {
  const lines = [];
  lines.push('VitaFlow - Personal Health Command Center (v' + VERSION + ')');
  lines.push('');
  lines.push('Usage: node tool.js <command> [options]');
  lines.push('');
  lines.push('Commands:');
  lines.push('  nutrition --meal "breakfast=oatmeal:50,banana,milk:250"   analyze a day of meals');
  lines.push('  bmr --sex male --age 30 --height 175 --weight 82 --activity light --goal lose');
  lines.push('  plan --goal muscle --days 3 --equipment bodyweight --level beginner');
  lines.push('  breathe --technique 478 --rounds 3 [--live] [--nowait]');
  lines.push('  sleep --bed 23:30 --rise 07:00 [--latency 15] [--wakeups 1] [--awake 10]');
  lines.push('  trend --csv path/to/data.csv   (or --csv-file with inline CSV)');
  lines.push('  report --demo                  full integrated wellness report');
  lines.push('  --self-test                    run the built-in test suite');
  lines.push('  --version                      print version');
  lines.push('  --help                         this help');
  lines.push('');
  lines.push('All tools print a medical disclaimer. This is not medical advice.');
  return lines.join('\n');
}

function getOpt(args, name) {
  const prefix = '--' + name + '=';
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--' + name && i + 1 < args.length) return args[i + 1];
    if (args[i].indexOf(prefix) === 0) return args[i].slice(prefix.length);
  }
  return null;
}

function hasFlag(args, name) {
  return args.indexOf('--' + name) !== -1;
}

function cmdNutrition(args) {
  const spec = getOpt(args, 'meal') || DEMO_MEAL;
  process.stdout.write(renderNutrition(analyzeMeal(spec)) + '\n');
}

function cmdBMR(args) {
  const sexR = checkSex(getOpt(args, 'sex') || 'male');
  if (!sexR.ok) { process.stderr.write('Error: ' + sexR.msg + '\n'); process.exitCode = 2; return; }
  const ageR = checkNumber('age', Number(getOpt(args, 'age') || 30), 18, 120, 'years');
  if (!ageR.ok) { process.stderr.write('Error: ' + ageR.msg + '\n'); process.exitCode = 2; return; }
  const hR = checkNumber('height', Number(getOpt(args, 'height') || 175), 120, 230, 'cm');
  if (!hR.ok) { process.stderr.write('Error: ' + hR.msg + '\n'); process.exitCode = 2; return; }
  const wR = checkNumber('weight', Number(getOpt(args, 'weight') || 82), 30, 250, 'kg');
  if (!wR.ok) { process.stderr.write('Error: ' + wR.msg + '\n'); process.exitCode = 2; return; }
  const aR = checkActivity(getOpt(args, 'activity') || 'light');
  if (!aR.ok) { process.stderr.write('Error: ' + aR.msg + '\n'); process.exitCode = 2; return; }
  const goal = getOpt(args, 'goal') || 'maintain';
  const gR = checkEnum('goal', goal, ['lose', 'maintain', 'gain']);
  if (!gR.ok) { process.stderr.write('Error: ' + gR.msg + '\n'); process.exitCode = 2; return; }
  process.stdout.write(renderBMR(sexR.value, ageR.value, hR.value, wR.value, aR.value, goal) + '\n');
}

function cmdPlan(args) {
  const goal = getOpt(args, 'goal') || 'muscle';
  const gR = checkEnum('goal', goal, ['strength', 'muscle', 'endurance']);
  if (!gR.ok) { process.stderr.write('Error: ' + gR.msg + '\n'); process.exitCode = 2; return; }
  const daysR = checkNumber('days', Number(getOpt(args, 'days') || 3), 2, 6, 'days');
  if (!daysR.ok) { process.stderr.write('Error: ' + daysR.msg + '\n'); process.exitCode = 2; return; }
  const eqR = checkEnum('equipment', getOpt(args, 'equipment') || 'bodyweight', ['bodyweight', 'gym', 'mixed']);
  if (!eqR.ok) { process.stderr.write('Error: ' + eqR.msg + '\n'); process.exitCode = 2; return; }
  const lvR = checkEnum('level', getOpt(args, 'level') || 'beginner', ['beginner', 'intermediate', 'advanced']);
  if (!lvR.ok) { process.stderr.write('Error: ' + lvR.msg + '\n'); process.exitCode = 2; return; }
  process.stdout.write(renderPlan(generatePlan(goal, daysR.value, eqR.value, lvR.value)) + '\n');
}

function cmdBreathe(args) {
  const tech = getOpt(args, 'technique') || '478';
  const tR = checkEnum('technique', tech, Object.keys(BREATH_TECHNIQUES));
  if (!tR.ok) { process.stderr.write('Error: ' + tR.msg + '\n'); process.exitCode = 2; return; }
  const roundsR = checkNumber('rounds', Number(getOpt(args, 'rounds') || 4), 1, 20, 'rounds');
  if (!roundsR.ok) { process.stderr.write('Error: ' + roundsR.msg + '\n'); process.exitCode = 2; return; }
  if (hasFlag(args, 'live')) {
    const r = breatheSession(tech, roundsR.value, true);
    process.stdout.write(r.lines + '\n');
    let step = 1;
    const total = roundsR.value * (BREATH_TECHNIQUES[tech].inhale + (BREATH_TECHNIQUES[tech].hold || 0) + BREATH_TECHNIQUES[tech].exhale + (BREATH_TECHNIQUES[tech].hold2 || 0));
    const timer = setInterval(function () {
      if (step > total) { clearInterval(timer); process.stdout.write('Session complete.\n'); return; }
      process.stdout.write('  [' + step + 's] ' + step + '\n');
      step++;
    }, 1000);
    return;
  }
  const nowait = hasFlag(args, 'nowait') || true;
  process.stdout.write(breatheSession(tech, roundsR.value, nowait).lines + '\n');
}

function cmdSleep(args) {
  const bed = getOpt(args, 'bed') || '23:30';
  const rise = getOpt(args, 'rise') || '07:00';
  const latencyR = checkNumber('latency', Number(getOpt(args, 'latency') || 15), 0, 120, 'minutes');
  const wakeupsR = checkNumber('wakeups', Number(getOpt(args, 'wakeups') || 1), 0, 20, 'wakeups');
  const awakeR = checkNumber('awake', Number(getOpt(args, 'awake') || 10), 0, 180, 'minutes');
  const checks = [latencyR, wakeupsR, awakeR].filter(function (c) { return !c.ok; });
  if (checks.length) { process.stderr.write('Error: ' + checks[0].msg + '\n'); process.exitCode = 2; return; }
  const r = analyzeSleep(bed, rise, latencyR.value, wakeupsR.value, awakeR.value);
  if (!r.ok) { process.stderr.write('Error: ' + r.msg + '\n'); process.exitCode = 2; return; }
  process.stdout.write(renderSleep(r) + '\n');
}

function cmdTrend(args) {
  let csv = getOpt(args, 'csv');
  if (!csv && hasFlag(args, 'demo')) csv = DEMO_CSV;
  if (!csv) {
    process.stderr.write('Error: provide --csv "date,weight,..." or use --demo\n');
    process.exitCode = 2; return;
  }
  process.stdout.write(renderTrend(csv) + '\n');
}

function cmdReport(args) {
  const o = {};
  if (getOpt(args, 'sex')) o.sex = String(getOpt(args, 'sex')).toLowerCase();
  if (getOpt(args, 'age')) o.age = Number(getOpt(args, 'age'));
  if (getOpt(args, 'height')) o.height = Number(getOpt(args, 'height'));
  if (getOpt(args, 'weight')) o.weight = Number(getOpt(args, 'weight'));
  if (getOpt(args, 'activity')) o.activity = String(getOpt(args, 'activity')).toLowerCase();
  if (getOpt(args, 'goal')) o.goal = String(getOpt(args, 'goal')).toLowerCase();
  if (getOpt(args, 'meal')) o.meal = getOpt(args, 'meal');
  process.stdout.write(renderReport(o) + '\n');
}

function main(argv) {
  const args = argv.slice(2);
  if (args.indexOf('--version') !== -1 || args[0] === 'version') {
    process.stdout.write('VitaFlow ' + VERSION + '\n'); return;
  }
  if (args.indexOf('--help') !== -1 || args[0] === 'help' || !args.length) {
    process.stdout.write(usage() + '\n'); return;
  }
  if (args.indexOf('--self-test') !== -1 || args[0] === '--self-test') {
    const r = runSelfTest();
    process.stdout.write('VitaFlow self-test: ' + r.pass + ' passed, ' + r.fail + ' failed\n');
    if (r.fail) {
      r.failures.forEach(function (f) { process.stderr.write('  FAIL: ' + f + '\n'); });
      process.exitCode = 1;
    } else {
      process.stdout.write('All checks passed.\n');
    }
    return;
  }
  const cmd = args[0];
  if (cmd === 'nutrition') return cmdNutrition(args);
  if (cmd === 'bmr') return cmdBMR(args);
  if (cmd === 'plan') return cmdPlan(args);
  if (cmd === 'breathe') return cmdBreathe(args);
  if (cmd === 'sleep') return cmdSleep(args);
  if (cmd === 'trend') return cmdTrend(args);
  if (cmd === 'report') return cmdReport(args);
  process.stderr.write('Unknown command: ' + cmd + '\n');
  process.stdout.write(usage() + '\n');
  process.exitCode = 2;
}

main(process.argv);
