// Статичні перевірки bridge, які інакше вилізли б лише всередині Live.
//
// Python не має компіляції, а вся логіка живе в процесі Ableton: помилка
// в назві ключа дзеркала дає KeyError десь на тіку, і побачити це можна
// хіба що в bridge.log. Тести з fake-live цього не ловлять -- вони ганяють
// JS-дзеркало, а не сам скрипт.

import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const src = readFileSync(join(root, 'remote-script/AbletonMP/AbletonMP.py'), 'utf8');

test('кожен ключ дзеркала оголошений при ініціалізації', () => {
  const init = src.match(/self\._mirror = \{([\s\S]*?)\n\s*\}/);
  assert.ok(init, 'ініціалізації _mirror не знайдено');
  const declared = new Set([...init[1].matchAll(/"([a-z_]+)":/g)].map((m) => m[1]));

  const used = new Set([...src.matchAll(/_mirror\[\s*"([a-z_]+)"\s*\]/g)].map((m) => m[1]));
  for (const m of src.matchAll(/_mirror\.get\(\s*"([a-z_]+)"/g)) used.add(m[1]);
  for (const m of src.matchAll(/_mirror\.setdefault\(\s*"([a-z_]+)"/g)) used.add(m[1]);

  const missing = [...used].filter((k) => !declared.has(k)).sort();
  assert.deepEqual(missing, [], `використовується, але не оголошено: ${missing.join(', ')}`);
});

test('кожен тип, який bridge емітить, він уміє й застосувати', () => {
  const emitted = new Set([...src.matchAll(/_emit\(\s*"([A-Za-z]+)"/g)].map((m) => m[1]));
  const listed = src.match(/APPLY_TYPES = \[([\s\S]*?)\]/);
  const declared = new Set([...listed[1].matchAll(/"([A-Za-z]+)"/g)].map((m) => m[1]));

  // RegistryInit -- не подія стану, її не застосовують через _apply
  emitted.delete('RegistryInit');
  const gap = [...emitted].filter((t) => !declared.has(t)).sort();
  assert.deepEqual(gap, [], `емітимо, але не приймаємо: ${gap.join(', ')}`);
});

test('константи переліків не розійшлися з валідацією', () => {
  // CLIP_PROPS перевіряється в _clip_prop_value: кожна властивість має
  // мати гілку, інакше вона мовчки не синхронізується
  const props = src.match(/CLIP_PROPS = \(([\s\S]*?)\)\n/);
  assert.ok(props, 'CLIP_PROPS не знайдено');
  const names = [...props[1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
  const validator = src.slice(src.indexOf('def _clip_prop_value'),
                              src.indexOf('def _clip_props_state'));
  const unchecked = names.filter((n) => !validator.includes(`"${n}"`));
  assert.deepEqual(unchecked, [], `у CLIP_PROPS без валідації: ${unchecked.join(', ')}`);
});
