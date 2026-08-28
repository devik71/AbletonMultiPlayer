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

test('у bridge немає методів, які нікому не потрібні', () => {
  // Мертвий код у Remote Script гірший за мертвий код деінде: він описує
  // механізм, якого вже немає, і читач вирішує, що той механізм працює.
  // Саме так сталося з автоемісією DeviceLoad -- її замінили на DeviceInsert,
  // а функція з докладним коментарем лишилась і брехала.
  const defs = [...src.matchAll(/^    def (_[a-z_0-9]+)\(/gm)].map((m) => m[1]);
  const dead = defs.filter((name) => src.split(`self.${name}`).length - 1 === 0);
  assert.deepEqual(dead, ['__init__'],
    `методи без викликів: ${dead.filter((n) => n !== '__init__').join(', ')}`);
});

// Верхня половина cp1251: символ на позиції i відповідає байту 0x80 + i.
// Node такого кодування не має, тож таблиця своя -- без неї перевірка
// нижче мовчки не спрацьовує ніколи. Перша версія саме такою й була:
// зелена і порожня, бо Buffer.from(..., 'win1251') просто кидав виняток.
const CP1251_HIGH = '\u0402\u0403\u201A\u0453\u201E\u2026\u2020\u2021\u20AC\u2030\u0409\u2039\u040A\u040C\u040B\u040F\u0452\u2018\u2019\u201C\u201D\u2022\u2013\u2014\uFFFF\u2122\u0459\u203A\u045A\u045C\u045B\u045F\u00A0\u040E\u045E\u0408\u00A4\u0490\u00A6\u00A7\u0401\u00A9\u0404\u00AB\u00AC\u00AD\u00AE\u0407\u00B0\u00B1\u0406\u0456\u0491\u00B5\u00B6\u00B7\u0451\u2116\u0454\u00BB\u0458\u0405\u0455\u0457\u0410\u0411\u0412\u0413\u0414\u0415\u0416\u0417\u0418\u0419\u041A\u041B\u041C\u041D\u041E\u041F\u0420\u0421\u0422\u0423\u0424\u0425\u0426\u0427\u0428\u0429\u042A\u042B\u042C\u042D\u042E\u042F\u0430\u0431\u0432\u0433\u0434\u0435\u0436\u0437\u0438\u0439\u043A\u043B\u043C\u043D\u043E\u043F\u0440\u0441\u0442\u0443\u0444\u0445\u0446\u0447\u0448\u0449\u044A\u044B\u044C\u044D\u044E\u044F';

const cp1251Byte = (ch) => {
  const code = ch.codePointAt(0);
  if (code < 0x80) return code;
  const i = CP1251_HIGH.indexOf(ch);
  return i < 0 ? -1 : i + 0x80;
};

// Кирилиця, прочитана як cp1251 і збережена ще раз як UTF-8, лишається
// валідним UTF-8: жоден парсер не свариться. Ловиться лише зворотним ходом.
function doubleEncoded(line) {
  const bytes = [];
  for (const ch of line) {
    const b = cp1251Byte(ch);
    if (b < 0) return false;          // cp1251 такого символу не знає
    bytes.push(b);
  }
  const back = Buffer.from(bytes).toString('utf8');
  if (back === line || back.includes('\uFFFD')) return false;
  return /[\u0400-\u04FF]/.test(back);
}

const mojibakeOf = (text) => [...Buffer.from(text, 'utf8')]
  .map((b) => (b < 0x80 ? String.fromCharCode(b) : CP1251_HIGH[b - 0x80]))
  .join('');

test('перевірка кодування справді ловить кашу', () => {
  // Без цього наступний тест може бути зелений просто тому, що нічого не
  // перевіряє -- рівно так і сталося з його першою версією.
  assert.ok(doubleEncoded(mojibakeOf('бутстрап реєстру')), 'каша має ловитись');
  assert.ok(!doubleEncoded('бутстрап реєстру'), 'чистий текст ловитись не має');
  assert.ok(!doubleEncoded('    def _prime_devices(self):'), 'ASCII ловитись не має');
});

test('українські рядки в bridge не побиті подвійним кодуванням', () => {
  // 127 рядків жили побитими крізь кілька релізів: тести лишались зелені,
  // а партнер посеред прогону читав у daemon кашу замість діагностики.
  const bad = [];
  src.split('\n').forEach((line, i) => {
    if (doubleEncoded(line)) bad.push(i + 1);
  });
  assert.deepEqual(bad, [],
    `подвійне кодування в рядках: ${bad.slice(0, 10).join(', ')}`);
});

test('перепідписка не робиться всередині callback-а слота', () => {
  // _rewire_tracks знімає ВСІ listener-и й вішає нові замикання. Викликана
  // з обробника слота, вона вбиває підписку сусіднього слота ДО того, як
  // Live встиг її викликати. Наслідок виміряний на живій парі: перетягування
  // кліпа зі слота в слот давало партнеру ДВА кліпи, бо ClipDelete не
  // народжувався взагалі. Всередині обробника дозволено лише _request_rewire.
  const from = src.indexOf('    def _on_slot_content(self');
  assert.ok(from > 0, '_on_slot_content не знайдено');
  const to = src.indexOf('\n    def ', from + 10);
  const body = src.slice(from, to);
  assert.ok(!/self\._rewire_tracks\(\)/.test(body),
    '_on_slot_content мусить просити перепідписку через _request_rewire');
  assert.ok(/self\._request_rewire\(\)/.test(body),
    '_on_slot_content мусить хоч колись просити перепідписку');

  // І сам тік мусить її виконувати -- інакше підписки не оновляться ніколи.
  const pump = src.slice(src.indexOf('    def _pump(self)'));
  assert.match(pump.slice(0, 900), /_rewire_pending/,
    '_pump мусить виконувати відкладену перепідписку');
});
