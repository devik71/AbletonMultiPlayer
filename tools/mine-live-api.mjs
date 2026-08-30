// Що з API вживає сам Ableton, а ми не бачили жодного разу.
//
// Live возить із собою понад тисячу власних Remote Script-ів -- Push, Move,
// Launchpad, Komplete. Це не приклади й не документація: це продакшн-код
// виробника, і він користується поверхнею, ширшою за довідник LOM.
//
// Ціна мовчанки довідника вже виміряна. Ми записали в COVERAGE.md, що
// автоматизацію синхронізувати неможливо, і збудували навколо цього цілий
// розділ vision.md з обходом через .als. Насправді метод є -- просто зветься
// create_automation_envelope, а не create_envelope. Знайшовся саме тут, за
// десять хвилин, і підтвердився на живому Live.
//
// Скрипти лежать скомпільованими. Розбирати marshal чужої версії Python
// ненадійно й непотрібно: імена лежать у константному пулі відкритим ASCII,
// і нам потрібна не точність переліку, а те, ЧОГО МИ НЕ БАЧИЛИ ЖОДНОГО РАЗУ.
// Хибнопозитивні тут дешеві -- пропущене відкриття коштує дорого.
//
//   node tools/mine-live-api.mjs [--root <тека>] [--min 3] [--all] [--json]
//
// Перевіряти знахідки ОБОВʼЯЗКОВО на живому Live: наявність імені в коді
// Ableton доводить, що метод існує в ЇХНІЙ збірці, а не в нашій.

import { readdirSync, readFileSync, statSync, existsSync } from 'node:fs';
import { join, basename, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, '..');

const DEFAULT_ROOTS = [
  'C:/ProgramData/Ableton/Live 12 Suite/Resources/MIDI Remote Scripts',
  'C:/ProgramData/Ableton/Live 12 Trial/Resources/MIDI Remote Scripts',
  '/Applications/Ableton Live 12 Suite.app/Contents/App-Resources/MIDI Remote Scripts',
];

const argv = process.argv.slice(2);
const flag = (name, fallback = null) => {
  const i = argv.indexOf(name);
  return i >= 0 && argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[i + 1] : fallback;
};
const MIN = Number(flag('--min', '3'));
const SHOW_ALL = argv.includes('--all');
const AS_JSON = argv.includes('--json');

const root = flag('--root') || DEFAULT_ROOTS.find((p) => existsSync(p));
if (!root) {
  console.error('не знайшов теку MIDI Remote Scripts.');
  console.error('вкажи явно: node tools/mine-live-api.mjs --root "<шлях>"');
  process.exit(1);
}

// ---------------------------------------------------------------- збір

function* walk(dir) {
  let entries;
  try { entries = readdirSync(dir); } catch { return; }
  for (const name of entries) {
    const full = join(dir, name);
    let st;
    try { st = statSync(full); } catch { continue; }
    if (st.isDirectory()) yield* walk(full);
    else yield full;
  }
}

const IDENT = /[a-zA-Z_][a-zA-Z0-9_]{3,44}/g;

function namesOf(buf) {
  // latin1: байти константного пулу читаються як символи один-в-один,
  // і жодне ім'я не губиться на невалідному UTF-8
  const out = new Set();
  const text = buf.toString('latin1');
  for (const m of text.matchAll(IDENT)) out.add(m[0]);
  return out;
}

const counts = new Map();          // ім'я -> у скількох файлах
const sample = new Map();          // ім'я -> де вперше побачили
let files = 0;

for (const path of walk(root)) {
  if (!path.endsWith('.pyc') && !path.endsWith('.py')) continue;
  files += 1;
  let buf;
  try { buf = readFileSync(path); } catch { continue; }
  const where = `${basename(dirname(path))}/${basename(path)}`;
  for (const n of namesOf(buf)) {
    counts.set(n, (counts.get(n) || 0) + 1);
    if (!sample.has(n)) sample.set(n, where);
  }
}

// ------------------------------------------------- що ми вже знаємо

const known = new Set();
function absorb(path) {
  let text;
  try { text = readFileSync(path, 'utf8'); } catch { return; }
  for (const m of text.matchAll(IDENT)) known.add(m[0]);
}
function absorbTree(dir, exts) {
  if (!existsSync(dir)) return;
  for (const p of walk(dir)) {
    if (exts.some((e) => p.endsWith(e))) absorb(p);
  }
}

absorb(join(REPO, 'remote-script/AbletonMP/AbletonMP.py'));
absorb(join(REPO, 'remote-script/AbletonMP/registry.py'));
absorbTree(join(REPO, '.claude/skills/ableton-lom'), ['.md', '.py']);
absorbTree(join(REPO, 'docs'), ['.md']);

// ------------------------------------------------------- фільтрація

// Імена, що пахнуть дією над документом. Не намагаємось бути точними --
// цей список лише піднімає цікаве вгору, повний перелік дає --all.
const JUICY = /(freeze|flatten|consolidat|crop|group|undo|redo|automat|envelope|take_lane|capture|quantiz|duplicat|record|overdub|punch|warp|browser|preset|chunk|selection|insert|delete|create|move_|swap|split|arm|monitor)/i;

// Нас цікавить поверхня LOM, а не фреймворк Ableton. Класи їхнього
// фреймворку -- CamelCase, а елементи керування впізнаються за суфіксом:
// record_button і quantize_button -- це кнопки на залізі, а не методи Live.
// Без цього фільтра перші тридцять рядків -- суцільні кнопки й скіни.
const LOMISH = /^[a-z][a-z0-9_]*$/;
const NOISE = new RegExp(
  '^(_+)?(on_|set_|update_|refresh_|make_|create_(default|button|control|skin|component)' +
  '|component|control|element|button|skin|color|midi_|sysex)'
  + '|(_(button|buttons|control|controls|component|components|element|elements'
  + '|skin|color|colors|layer|layers|mode|modes|task|tasks|slot|slots_raw|notifier))$');

const rows = [];
for (const [name, c] of counts) {
  if (c < MIN) continue;
  if (known.has(name)) continue;
  if (!LOMISH.test(name)) continue;
  if (NOISE.test(name)) continue;
  if (!SHOW_ALL && !JUICY.test(name)) continue;
  rows.push({ name, files: c, seen: sample.get(name) });
}
rows.sort((a, b) => b.files - a.files || a.name.localeCompare(b.name));

// ----------------------------------------------------------- вивід

if (AS_JSON) {
  console.log(JSON.stringify({ root, files, unique: counts.size, rows }, null, 2));
} else {
  console.log(`тека   : ${root}`);
  console.log(`файлів : ${files}, унікальних імен: ${counts.size}, знаємо: ${known.size}`);
  console.log(`поріг  : у ${MIN}+ файлах${SHOW_ALL ? ', без фільтра за темою' : ''}`);
  console.log('');
  console.log(`=== вживає Ableton, не бачили ми: ${rows.length} ===`);
  for (const r of rows.slice(0, 120)) {
    console.log(`${String(r.files).padStart(5)}  ${r.name.padEnd(44)} ${r.seen}`);
  }
  if (rows.length > 120) console.log(`... ще ${rows.length - 120}, повний перелік через --json`);
  console.log('');
  console.log('Кожну знахідку перевіряти на живому Live: виклик без аргументів');
  console.log('віддає C++ підпис у тексті помилки -- це найшвидший спосіб');
  console.log('дізнатись, що метод існує і що він приймає.');
}
