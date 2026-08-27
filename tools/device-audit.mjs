// Що стокові девайси показують ПОВЗ parameters.
//
// Параметри в нас покриті всі: DeviceParamSet не знає про конкретний девайс,
// він возить будь-який DeviceParameter будь-чого. А от обʼєктний стан повз
// parameters -- ні: саме там знайшовся `sample` у Simpler, через який маркери
// на хвилі не синхронізувались.
//
// Тож замість того, щоб обходити шістдесят девайсів здогадом, ця проба
// завантажує кожен у тимчасовий трек і питає в Live, що він показує.
// Результат -- виміряний перелік робіт, а не гадання.
//
// Слід у сеті: один трек, який прибирається в кінці. Демон варто зупинити,
// інакше кожне завантаження полетить партнеру подією.
//
//   node tools/device-audit.mjs [--categories audio_effects,instruments]
//                               [--limit 200] [--out docs/device-audit.md]

import { readFileSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

const arg = (name, fallback) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 ? process.argv[i + 1] : fallback;
};

const CATEGORIES = arg('categories', 'audio_effects,instruments,midi_effects').split(',');
const LIMIT = Number(arg('limit', 500));
const OUT = arg('out', 'docs/device-audit.md');

const token = readFileSync(join(homedir(), '.abletonmp', 'chat_token'), 'utf8').trim();

async function exec(actions) {
  const res = await fetch('http://127.0.0.1:19847/api/exec', {
    method: 'POST',
    headers: { 'X-AbletonMP-Token': token, 'Content-Type': 'application/json' },
    body: JSON.stringify({ actions }),
  });
  return res.json();
}
const one = (r) => r?.result?.results?.[0];
const val = (r) => one(r)?.result;

/** Те, що є в КОЖНОГО девайса. Цікаве -- усе, чого тут немає. */
const COMMON_OBJECTS = new Set(['canonical_parent', 'view']);
const COMMON_COLLECTIONS = new Set(['parameters']);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// --------------------------------------------------------------------------

console.log('шукаю стокові девайси в браузері…');
const items = [];
for (const category of CATEGORIES) {
  const r = await exec([{ op: 'lom_get', path: ['app', 'browser', category, 'children'] }]);
  const kids = val(r);
  if (!Array.isArray(kids)) {
    console.log(`  ${category}: не читається (${one(r)?.error || 'без дітей'})`);
    continue;
  }
  for (const kid of kids) {
    if (kid?.name) items.push({ category, name: kid.name });
  }
  console.log(`  ${category}: ${kids.length}`);
}
if (!items.length) {
  console.error('браузер нічого не віддав — аудит без Live не має сенсу');
  process.exit(2);
}

// Тимчасовий трек: сет належить людині, слідів по собі лишати не можна.
const before = (val(await exec([{ op: 'lom_get', path: ['tracks'] }])) || []).length;
await exec([{ op: 'create_track', kind: 'midi', name: 'AUDIT-TEMP' }]);
await sleep(1200);
const tracks = val(await exec([{ op: 'lom_get', path: ['tracks'] }])) || [];
const idx = tracks.findIndex((t) => t?.name === 'AUDIT-TEMP');
if (idx < 0) {
  console.error('тимчасовий трек не створився');
  process.exit(2);
}
console.log(`тимчасовий трек #${idx}, девайсів до аудиту ${before}\n`);

const rows = [];
let failed = 0;

try {
  for (const item of items.slice(0, LIMIT)) {
    // Кожен девайс окремо: вантажимо, дивимось, прибираємо.
    // load_device лише СТАВИТЬ У ЧЕРГУ: bridge вантажить по одному за тік,
    // бо важкий інструмент блокує Live на сотні мілісекунд. Тож чекаємо на
    // появу девайса, а не на фіксовану паузу.
    const load = await exec([{ op: 'load_device', track_index: idx, name: item.name,
                               category: item.category }]);
    if (!one(load)?.ok) {
      failed += 1;
      console.log(`  ! ${item.name.padEnd(28)} не поставився в чергу: ${one(load)?.error || '?'}`);
      continue;
    }
    let devices = [];
    const deadline = Date.now() + 12000;
    while (Date.now() < deadline) {
      await sleep(500);
      devices = val(await exec([{ op: 'lom_get', path: ['tracks', idx, 'devices'] }])) || [];
      if (devices.length) break;
    }
    if (!devices.length) {
      failed += 1;
      console.log(`  ! ${item.name.padEnd(28)} не завантажився`);
      continue;
    }
    const last = devices.length - 1;

    const info = val(await exec([{ op: 'lom_dir', path: ['tracks', idx, 'devices', last] }]));
    if (info) {
      const objects = (info.objects || []).filter((x) => !COMMON_OBJECTS.has(x.split(':')[0]));
      const collections = (info.collections || [])
        .filter((x) => !COMMON_COLLECTIONS.has(x.split('[')[0]));
      const params = (info.collections || []).find((x) => x.startsWith('parameters['));
      rows.push({
        name: item.name,
        category: item.category,
        lom: info.lom_type,
        params: params ? params.replace(/\D+/g, '') : '?',
        objects,
        collections,
        scalars: info.scalars || [],
      });
      const extra = [...objects, ...collections];
      console.log(`  ${item.name.padEnd(28)} ${extra.length ? extra.join(', ') : '—'}`);
    }

    await exec([{ op: 'lom_call', path: ['tracks', idx], method: 'delete_device',
                  args: [last] }]);
    await sleep(400);
  }
} finally {
  await exec([{ op: 'delete_track', track_index: idx }]);
  console.log('\nтимчасовий трек прибрано');
}

// --------------------------------------------------------------------------

// Спільні скаляри є в кожного девайса (name, class_name, is_active...).
// Цікаве -- те, що є не в усіх: саме там ховається стан на кшталт
// playback_mode у Simpler, якого серед parameters немає.
const common = rows.length
  ? rows.map((r) => new Set(r.scalars)).reduce((a, b) => new Set([...a].filter((x) => b.has(x))))
  : new Set();
// Шум серед скалярів: кнопка A/B у вікні девайса, ознаки «чи можна»,
// стан розгортання UI і позиція відтворення. Нічого з цього не є станом
// документа, який має сенс возити партнеру.
const SCALAR_NOISE = /^is_using_compare_preset_b$|^can_|^has_|^is_showing_|^visible_macro_count$|^playing_position/;
for (const r of rows) {
  r.own = (r.scalars || []).filter((x) => !common.has(x) && !SCALAR_NOISE.test(x));
}

// Списки можливих значень (*_list) -- це не стан, а вміст випадайки.
// Маршрутизація девайса -- окрема тема: залізо в кожного своє.
const NOISE = /_list$|^audio_(in|out)puts$|^midi_(in|out)puts$|^macros_mapped$/;
for (const r of rows) {
  r.extra = [...r.objects, ...r.collections].filter((x) => !NOISE.test(x.split(/[[:]/)[0]));
}

const interesting = rows.filter((r) => r.extra.length || r.own.length);
const lines = [
  '# Що стокові девайси показують повз `parameters`',
  '',
  '> Згенеровано `node tools/device-audit.mjs` на живому Live.',
  `> Девайсів опитано: ${rows.length}, не завантажилось: ${failed}.`,
  '',
  'Параметри покриті `DeviceParamSet`-ом усі -- він не знає про конкретний',
  'девайс. Тут перелічене те, чого серед `parameters` немає: саме такий стан',
  'ми досі втрачали, і саме через нього маркери семплу в Simpler не їхали.',
  '',
  `## Девайси з обʼєктним станом (${interesting.length})`,
  '',
  'Відсіяно як шум: списки можливих значень (*_list -- це вміст випадайки,',
  'а не стан), маршрутизація девайса (audio_inputs, midi_outputs -- залізо',
  'в кожного своє) і macros_mapped (похідне від параметрів).',
  '',
  '| Девайс | Категорія | Параметрів | Обʼєкти й колекції | Власні скаляри |',
  '|---|---|---:|---|---|',
];
for (const r of interesting.sort((a, b) => a.name.localeCompare(b.name))) {
  lines.push(`| ${r.name} | ${r.category} | ${r.params} | ${r.extra.join(", ") || "—"} | ${r.own.join(", ") || "—"} |`);
}
lines.push('', '### Спільні скаляри, які має кожен девайс', '',
  [...common].sort().join(", ") || "—");
lines.push('',
  '**Межа цієї проби.** Девайс опитується щойно завантаженим, тобто порожнім.',
  'Стан, який зʼявляється лише разом із вмістом, вона не побачить: у Simpler',
  'без семплу немає обʼєкта sample -- того самого, через який маркери на хвилі',
  'й не синхронізувались. Порожній рядок тут означає «нічого понад параметри',
  'у ПОРОЖНЬОМУ девайсі», а не «нічого взагалі».');
lines.push('', `## Решта (${rows.length - interesting.length}) -- лише параметри`, '');
lines.push(rows.filter((r) => !r.extra.length && !r.own.length)
  .map((r) => r.name).sort().join(', ') || '—');
lines.push('');

writeFileSync(OUT, lines.join('\n'));
console.log(`\nопитано ${rows.length}, з обʼєктним станом ${interesting.length}, звіт у ${OUT}`);
