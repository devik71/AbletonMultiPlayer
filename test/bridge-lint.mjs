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

test('кожна базова лінія встановлюється в ОБОХ шляхах бутстрапу', () => {
  // Дисципліна, що вже двічі коштувала нам мовчазного бага: дерево девайсів
  // і властивості кліпів лишались без базової лінії там, де про них забули.
  // Реєстр піднімається двома шляхами -- створення й прийняття чужого, --
  // і пропуск в одному з них видно лише на живому Live.
  const primes = new Set(
    [...src.matchAll(/def (_prime_[a-z_]+)\(self\)/g)].map((m) => m[1]),
  );
  assert.ok(primes.size >= 8, `надто мало _prime_*: ${primes.size}`);

  const bodyOf = (name) => {
    const start = src.indexOf(`    def ${name}(self`);
    assert.ok(start > 0, `${name} не знайдено`);
    const end = src.indexOf('\n    def ', start + 10);
    return src.slice(start, end > 0 ? end : undefined);
  };
  const build = bodyOf('_build_registry');
  const adopt = bodyOf('_adopt_registry');

  // Ті, що приймають аргументи, викликаються точково -- їх не рахуємо
  const global = [...primes].filter((n) => new RegExp(`${n}\(\)`).test(src));
  // Викликана з іншого праймера теж рахується: важлива базова лінія,
  // а не те, звідки саме її встановили.
  const reachable = (body) => {
    const direct = global.filter((n) => body.includes(`${n}()`));
    const nested = global.filter((n) => direct.some((d) => bodyOf(d).includes(`${n}()`)));
    return new Set([...direct, ...nested]);
  };
  const inBuild = reachable(build);
  const inAdopt = reachable(adopt);
  const missing = global.filter((n) => !inBuild.has(n) || !inAdopt.has(n));
  assert.deepEqual(missing.sort(), [],
    `не в обох шляхах бутстрапу: ${missing.join(', ')}`);
});

test('усе, що _apply вміє застосувати, оголошено в APPLY_TYPES', () => {
  const applied = new Set();
  for (const m of src.matchAll(/etype == "([A-Za-z]+)"/g)) applied.add(m[1]);
  const listed = src.match(/APPLY_TYPES = \[([\s\S]*?)\]/);
  const declared = new Set([...listed[1].matchAll(/"([A-Za-z]+)"/g)].map((m) => m[1]));
  const undeclared = [...applied].filter((t) => !declared.has(t)).sort();
  assert.deepEqual(undeclared, [],
    `застосовуємо, але не оголошуємо -- партнер не знатиме: ${undeclared.join(', ')}`);
});

test('праймери, що ЧИСТЯТЬ дзеркало, завжди тягнуть за собою лінійку', () => {
  // Тонке місце. _prime_notes, _prime_all_clip_props і _prime_all_clip_warp
  // починаються з очищення свого словника -- а в тих самих словниках живуть
  // записи кліпів ЛІНІЙКИ під ключем arr:<uuid>. Пропустити після них
  // _prime_arrangement_clips означає лишити лінійку без базової лінії,
  // і найгірший наслідок саме в нотах: правка виглядатиме як базова лінія
  // і не поїде взагалі.
  const wipers = ['_prime_notes', '_prime_all_clip_props', '_prime_all_clip_warp'];
  for (const name of wipers) {
    const body = src.slice(src.indexOf(`    def ${name}(self`));
    assert.match(body.slice(0, 400), /= \{\}/, `${name} мав би чистити словник`);
  }

  const lines = src.split('\n');
  const orphans = [];
  lines.forEach((line, i) => {
    if (!wipers.includes(line.trim().replace(/^self\.|\(\)$/g, ''))) return;
    const near = lines.slice(i, i + 12).join('\n');
    if (!near.includes('_prime_arrangement_clips()')) orphans.push(i + 1);
  });
  assert.deepEqual(orphans, [],
    `чистять дзеркало без відновлення лінійки, рядки: ${orphans.join(', ')}`);
});
