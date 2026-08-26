// Знімок розкладають дві реалізації: _state_to_ops у Remote Script (вона й
// працює в живому Live) і daemon/tools/state-ops.js (нею користуються демон,
// fake-live і решта тестів). Розійтись вони можуть тихо: тести ганяють JS,
// а в Live виконується Python, тож ціла гілка знімка місяцями «працює»
// в тестах і не працює насправді. Саме так і сталося з ClipPropSet,
// ClipWarpSet, SongPropSet, CueSet, SceneTimingSet, ChainMixerSet
// і всією лінійкою.
//
// Тест статичний навмисно: Python тут не запустити, зате перелік типів,
// які кожна сторона вміє покласти в ops, видно з тексту.

import test from 'node:test';
import assert from 'node:assert';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');

const bridge = readFileSync(join(root, 'remote-script/AbletonMP/AbletonMP.py'), 'utf8');
const mirror = readFileSync(join(root, 'daemon/tools/state-ops.js'), 'utf8');

// Межі: від _state_to_ops до першого хелпера, що вже не про розкладку.
const slice = (text, from, to) => {
  const start = text.indexOf(from);
  const end = text.indexOf(to, start);
  assert.ok(start >= 0 && end > start, `не знайдено ділянку ${from}..${to}`);
  return text.slice(start, end);
};

const typesFrom = (text, re) => {
  const found = new Set();
  for (const m of text.matchAll(re)) found.add(m[1]);
  return found;
};

test('знімок розкладається в ті самі типи подій у bridge і в дзеркалі', () => {
  const pyOps = slice(bridge, 'def _state_to_ops', 'def _note_regions_for');
  const py = typesFrom(pyOps, /\(\("([A-Z][A-Za-z]+)",/g);
  const js = typesFrom(mirror, /\['([A-Z][A-Za-z]+)',/g);

  assert.ok(py.size >= 10, `у bridge знайдено лише ${py.size} типів -- ділянка зрізана не там`);

  const onlyJs = [...js].filter((t) => !py.has(t)).sort();
  const onlyPy = [...py].filter((t) => !js.has(t)).sort();
  assert.deepStrictEqual(onlyJs, [],
    `дзеркало кладе в знімок те, чого bridge не кладе: ${onlyJs.join(', ')}`);
  assert.deepStrictEqual(onlyPy, [],
    `bridge кладе в знімок те, чого дзеркало не кладе: ${onlyPy.join(', ')}`);
});

test('кожна гілка знімка в bridge читає ту саму назву поля, що й дзеркало', () => {
  const pyOps = slice(bridge, 'def _state_to_ops', 'def _note_regions_for');
  // Поля верхнього рівня знімка: якщо bridge читає state.get("chains"),
  // а дзеркало state.chains -- імена мусять збігатись, інакше одна зі
  // сторін мовчки бачить порожнечу.
  for (const field of ['song', 'chains', 'cues', 'arrangement', 'aux_tracks', 'scenes']) {
    assert.ok(pyOps.includes(`"${field}"`), `bridge не читає поле знімка ${field}`);
    assert.ok(mirror.includes(`.${field}`), `дзеркало не читає поле знімка ${field}`);
  }
});
