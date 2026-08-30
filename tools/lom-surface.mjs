// Повна поверхня LOM живого Live -- і що з неї ми не чіпаємо.
//
// Довго ми вгадували назви: шукали create_envelope, а метод зветься
// create_automation_envelope, і через це автоматизація роками вважалась
// недосяжною. Вгадування закінчується тут. dir() на живому обʼєкті віддає
// ВСЮ його поверхню, тож питання "що вміє Live" стає перелічуваним.
//
// Інструмент робить три речі:
//   1. обходить дерево обʼєктів Live і збирає їхні методи й властивості;
//   2. віднімає те, що вже згадане в нашому коді й документації;
//   3. показує різницю -- це і є перелік невикористаних можливостей.
//
//   node tools/lom-surface.mjs                 різниця, згрупована за типом
//   node tools/lom-surface.mjs --all           уся поверхня, не лише різниця
//   node tools/lom-surface.mjs --type Clip     лише один тип
//   node tools/lom-surface.mjs --json          машиночитно
//
// Потрібен запущений Live із нашим Remote Script (порт 19847).

import { readFileSync, existsSync, readdirSync, statSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { homedir } from 'node:os';
import { fileURLToPath } from 'node:url';

const REPO = join(dirname(fileURLToPath(import.meta.url)), '..');
const argv = process.argv.slice(2);
const flag = (n, d = null) => {
  const i = argv.indexOf(n);
  return i >= 0 && argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[i + 1] : d;
};
const SHOW_ALL = argv.includes('--all');
const AS_JSON = argv.includes('--json');
const ONLY = flag('--type');

const TOKEN = readFileSync(join(homedir(), '.abletonmp', 'chat_token'), 'utf8').trim();
const URL = 'http://127.0.0.1:19847/api/exec';

async function exec(actions) {
  const res = await fetch(URL, {
    method: 'POST',
    headers: { 'X-AbletonMP-Token': TOKEN, 'Content-Type': 'application/json' },
    body: JSON.stringify({ actions }),
  });
  const data = await res.json();
  return data?.result?.results ?? [];
}
const first = async (a) => (await exec(a))[0] ?? {};
const dirOf = async (path) => (await first([{ op: 'lom_dir', path }]))?.result ?? null;

// Дерево обходу. Свідомо не рекурсивне вглиб: нас цікавлять ТИПИ, а не
// кожен екземпляр, тож достатньо по одному представнику кожного.
const SEEDS = [
  ['Song', ['song']],
  ['Application', ['app']],
  ['ApplicationView', ['app', 'view']],
  ['Browser', ['app', 'browser']],
  ['BrowserItem', ['app', 'browser', 'drums', 'children', 0]],
  ['SongView', ['view']],
  ['Track', ['tracks', 0]],
  ['TrackView', ['tracks', 0, 'view']],
  ['MixerDevice', ['tracks', 0, 'mixer_device']],
  ['DeviceParameter', ['tracks', 0, 'mixer_device', 'volume']],
  ['ClipSlot', ['tracks', 0, 'clip_slots', 0]],
  ['Scene', ['scenes', 0]],
  ['MasterTrack', ['master_track']],
  ['ReturnTrack', ['return_tracks', 0]],
  ['CuePoint', ['cue_points', 0]],
];

// Те, що ми вже знаємо: власний код і вся документація.
function collectKnown() {
  const known = new Set();
  const IDENT = /[a-zA-Z_][a-zA-Z0-9_]{2,48}/g;
  const eat = (p) => {
    let t;
    try { t = readFileSync(p, 'utf8'); } catch { return; }
    for (const m of t.matchAll(IDENT)) known.add(m[0]);
  };
  const walk = (dir, exts) => {
    if (!existsSync(dir)) return;
    for (const n of readdirSync(dir)) {
      const full = join(dir, n);
      if (statSync(full).isDirectory()) walk(full, exts);
      else if (exts.some((e) => full.endsWith(e))) eat(full);
    }
  };
  eat(join(REPO, 'remote-script/AbletonMP/AbletonMP.py'));
  eat(join(REPO, 'remote-script/AbletonMP/registry.py'));
  walk(join(REPO, 'docs'), ['.md']);
  walk(join(REPO, '.claude/skills/ableton-lom'), ['.md']);
  return known;
}

const known = collectKnown();
const seen = new Map();   // тип -> { methods, scalars, objects, collections }

for (const [label, path] of SEEDS) {
  if (ONLY && !label.toLowerCase().includes(ONLY.toLowerCase())) continue;
  const d = await dirOf(path);
  if (!d || !d.lom_type) continue;
  if (!seen.has(d.lom_type)) seen.set(d.lom_type, { label, ...d });
  // Обʼєктні поля -- ще один рівень: там ховаються Envelope, Chain, DrumPad
  for (const entry of d.objects ?? []) {
    const name = String(entry).split(':')[0];
    const sub = await dirOf([...path, name]);
    if (sub?.lom_type && !seen.has(sub.lom_type)) {
      seen.set(sub.lom_type, { label: `${label}.${name}`, ...sub });
    }
  }
  for (const entry of d.collections ?? []) {
    const m = /^(.+)\[(\d+)\]$/.exec(String(entry));
    if (!m || Number(m[2]) === 0) continue;
    const sub = await dirOf([...path, m[1], 0]);
    if (sub?.lom_type && !seen.has(sub.lom_type)) {
      seen.set(sub.lom_type, { label: `${label}.${m[1]}[0]`, ...sub });
    }
  }
}

const report = [];
for (const [type, d] of seen) {
  const unused = {
    methods: (d.methods ?? []).filter((n) => !known.has(n)),
    scalars: (d.scalars ?? []).filter((n) => !known.has(n)),
    objects: (d.objects ?? []).filter((n) => !known.has(String(n).split(':')[0])),
    collections: (d.collections ?? []).filter((n) => !known.has(String(n).split('[')[0])),
  };
  report.push({ type, via: d.label, all: d, unused });
}
report.sort((a, b) => {
  const n = (r) => r.unused.methods.length + r.unused.scalars.length;
  return n(b) - n(a) || a.type.localeCompare(b.type);
});

if (AS_JSON) {
  console.log(JSON.stringify(report, null, 2));
} else {
  console.log(`типів обійдено: ${report.length}, знаємо імен: ${known.size}`);
  console.log('');
  for (const r of report) {
    const src = SHOW_ALL ? r.all : r.unused;
    const total = (src.methods ?? []).length + (src.scalars ?? []).length
      + (src.objects ?? []).length + (src.collections ?? []).length;
    if (!total) continue;
    console.log(`=== ${r.type}  (через ${r.via}) ===`);
    for (const [key, title] of [['methods', 'методи'], ['scalars', 'властивості'],
                                ['objects', 'обʼєкти'], ['collections', 'колекції']]) {
      const list = src[key] ?? [];
      if (!list.length) continue;
      console.log(`  ${title}: ${list.join(', ')}`);
    }
    console.log('');
  }
  if (!SHOW_ALL) {
    console.log('Показано лише те, чого немає ні в нашому коді, ні в docs/.');
    console.log('Уся поверхня -- через --all. Перевіряти наживо: виклик без');
    console.log('аргументів віддає C++ підпис у тексті помилки.');
  }
}
