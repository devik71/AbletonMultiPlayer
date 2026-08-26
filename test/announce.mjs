// Оголошений перелік типів мусить збігатися з тим, що код справді застосовує.
//
// Розбіжність не ламає збірку і не видно в E2E: обидва боки працюють, доки
// не зустрінуться. А зустрівшись, relay каже «партнер не вміє X» -- і люди
// йдуть шукати неіснуючу проблему. Саме так і сталось: fake-live застосовував
// чотири Arrangement-типи, жодного разу про них не повідомивши.

import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');

/** Тіло функції apply() у дзеркалі -- від оголошення до наступної функції. */
function applyBody(src) {
  const start = src.indexOf('function apply(type, payload, gseq)');
  assert.ok(start > 0, 'функції apply у fake-live немає');
  const end = src.indexOf('\nfunction ', start + 10);
  return src.slice(start, end > 0 ? end : undefined);
}

test('fake-live оголошує рівно ті типи, які застосовує', () => {
  const src = readFileSync(join(root, 'daemon/tools/fake-live.js'), 'utf8');

  const listed = src.match(/events: arg\('events',\s*([\s\S]*?)\)\.split/);
  assert.ok(listed, 'перелік events не знайдено');
  const announced = new Set(
    listed[1].replace(/['"+\s]/g, '').split(',').filter(Boolean),
  );

  const applied = new Set(
    [...applyBody(src).matchAll(/case '([A-Z][A-Za-z]+)':/g)].map((m) => m[1]),
  );

  const missing = [...applied].filter((t) => !announced.has(t)).sort();
  const extra = [...announced].filter((t) => !applied.has(t)).sort();
  assert.deepEqual(missing, [], `застосовує, але не оголошує: ${missing.join(', ')}`);
  assert.deepEqual(extra, [], `оголошує, але не застосовує: ${extra.join(', ')}`);
});

test('bridge оголошує рівно ті типи, які застосовує', () => {
  const src = readFileSync(join(root, 'remote-script/AbletonMP/AbletonMP.py'), 'utf8');

  const listed = src.match(/APPLY_TYPES = \[([\s\S]*?)\]/);
  assert.ok(listed, 'APPLY_TYPES не знайдено');
  const announced = new Set(
    [...listed[1].matchAll(/"([A-Za-z]+)"/g)].map((m) => m[1]),
  );

  // Гілки _apply: elif etype == "X" та etype in ("X", "Y")
  const applied = new Set();
  for (const m of src.matchAll(/etype == "([A-Za-z]+)"/g)) applied.add(m[1]);
  for (const m of src.matchAll(/etype in \(([^)]*)\)/g)) {
    for (const q of m[1].matchAll(/"([A-Za-z]+)"/g)) applied.add(q[1]);
  }

  const missing = [...announced].filter((t) => !applied.has(t)).sort();
  assert.deepEqual(missing, [], `оголошує, але не застосовує: ${missing.join(', ')}`);
});

test('довідка емулятора перелічує всі його команди', () => {
  // Під час прогону парою людина шукає в довідці спосіб відтворити баг.
  // Команда, якої там немає, вважається неіснуючою -- так довідка відстала
  // на девʼятнадцять команд і не згадувала ні лінійку, ні семпли, ні локатори.
  const src = readFileSync(join(root, 'daemon/tools/fake-live.js'), 'utf8');
  const commands = [...src.matchAll(/^ {4}case '([a-z]+)':/gm)].map((m) => m[1]);
  assert.ok(commands.length >= 40, `команд знайдено лише ${commands.length}`);

  const from = src.lastIndexOf('console.log([');
  const help = src.slice(from, src.indexOf('].join(', from));
  assert.ok(help.length > 200, 'довідку не знайдено');

  // Розбираємо довідку на слова, а не шукаємо підрядок: інакше «note»
  // зарахувалось би через «delnote», і половина команд рахувалась би
  // описаною, не бувши описаною.
  const words = new Set(help.split(/[^a-z]+/i).filter(Boolean));
  const silent = [...new Set(commands.filter((c) => !words.has(c)))].sort();
  assert.deepEqual(silent, [], `команда є, але в довідці її немає: ${silent.join(', ')}`);
});
