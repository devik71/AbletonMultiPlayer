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
const mirror2 = readFileSync(join(root, 'daemon/tools/fake-live.js'), 'utf8');

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

test('знімок bridge і знімок емулятора мають ті самі поля верхнього рівня', () => {
  // Якщо bridge не кладе в знімок цілу гілку, порівняння `diff` і застосування
  // pull мовчки бачать порожнечу -- рівно так знімок місяцями їхав без
  // song, cues і chains, а тести цього не помічали: fullState емулятора їх мав.
  const py = slice(bridge, 'return {\n            "version": STATE_VERSION',
    '    def _state_mixer');
  const js = slice(mirror2, 'const fullState = () => ({', '\n});');

  const pyKeys = new Set([...py.matchAll(/^\s{12}"([a-z_]+)":/gm)].map((m) => m[1]));
  const jsKeys = new Set([...js.matchAll(/^\s{2}([a-z_]+):/gm)].map((m) => m[1]));
  assert.ok(pyKeys.size >= 8, `у bridge знайдено лише ${pyKeys.size} полів знімка`);

  const onlyJs = [...jsKeys].filter((k) => !pyKeys.has(k)).sort();
  const onlyPy = [...pyKeys].filter((k) => !jsKeys.has(k)).sort();
  assert.deepStrictEqual(onlyJs, [],
    `емулятор кладе в знімок те, чого bridge не кладе: ${onlyJs.join(', ')}`);
  assert.deepStrictEqual(onlyPy, [],
    `bridge кладе в знімок те, чого емулятор не кладе: ${onlyPy.join(', ')}`);
});

test('прогалини описані з обох боків і мають людський текст у демоні', () => {
  // Прогалина, яку вміє назвати лише одна сторона, -- це або мовчазна
  // втрата («застосовано» замість «бракує»), або рядок звіту виду
  // [object Object]. Обидва варіанти гірші за падіння тесту.
  const daemon = readFileSync(join(root, 'daemon/index.js'), 'utf8');
  const py = new Set([...bridge.matchAll(/"what":\s*"([a-z_]+)"/g)].map((m) => m[1]));
  const js = new Set([...mirror2.matchAll(/what:\s*'([a-z_]+)'/g)].map((m) => m[1]));
  const described = new Set([...daemon.matchAll(/gap\.what === '([a-z_]+)'/g)].map((m) => m[1]));

  assert.ok(py.size >= 8, `у bridge знайдено лише ${py.size} видів прогалин`);
  assert.deepStrictEqual([...js].filter((k) => !py.has(k)).sort(), [],
    'емулятор знає прогалину, якої не знає bridge');
  assert.deepStrictEqual([...py].filter((k) => !js.has(k)).sort(), [],
    'bridge знає прогалину, якої не знає емулятор');
  assert.deepStrictEqual([...py].filter((k) => !described.has(k)).sort(), [],
    'демон не має тексту для прогалини');
});

test('кожен застосовний тип названий у документації', () => {
  // Документація, що відстала від коду, гірша за відсутню: людина шукає
  // подію в переліку, не знаходить і робить висновок, що її немає.
  // Так README цілий реліз описував стан на девʼять типів раніше.
  const docs = ['README.md', 'docs/COVERAGE.md', 'docs/PROTOCOL.md']
    .map((p) => readFileSync(join(root, p), 'utf8')).join('\n');
  const block = bridge.slice(bridge.indexOf('APPLY_TYPES'), bridge.indexOf('CLIP_PROPS'));
  const applied = [...new Set([...block.matchAll(/"([A-Z][A-Za-z]+)"/g)].map((m) => m[1]))];
  assert.ok(applied.length >= 20, `APPLY_TYPES прочитано не повністю: ${applied.length}`);
  const silent = applied.filter((t) => !docs.includes(t)).sort();
  assert.deepStrictEqual(silent, [], `тип є в коді, але не названий у docs: ${silent.join(', ')}`);
});

test('підполя треку й кліпа в знімку звуться однаково з обох боків', () => {
  // Наявні перевірки стежили лише за ВЕРХНІМ рівнем знімка. Розійтись же
  // легше всередині: коли bridge почав класти в трек "routing", а в кліп
  // "envelopes", емулятор про них не знав -- і знімок мовчки їхав без
  // маршрутів і автоматизації. Верхній рівень при цьому збігався ідеально.
  //
  // Шукаємо саме в БУДІВНИКАХ знімка, а не по всьому файлу: перша версія
  // цієї перевірки була зелена й порожня, бо "routing:" знаходилось у
  // заготовці треку емулятора, а не в тому місці, де збирається знімок.
  const pyTrack = slice(bridge, 'tracks.append({', '            })');
  const pyClip = slice(bridge, '    def _state_clips(self, track, scenes):',
    '    # ----------------------------------------------------------- state apply');
  const jsSnap = slice(mirror2, 'const fullState = () => ({', '\n});');
  const jsClip = slice(mirror2, 'const clipShape = (clip) => {', '\n};');
  const jsOps = mirror;   // clipOps і stateToOps лежать в одному файлі

  for (const field of ['mixer', 'devices', 'clips', 'routing']) {
    assert.match(pyTrack, new RegExp(`"${field}":`), `bridge не кладе в трек ${field}`);
    assert.match(jsSnap, new RegExp(`^\\s+${field}:`, 'm'), `емулятор не кладе в трек ${field}`);
    assert.match(jsOps, new RegExp(`track\\.${field}`), `state-ops не читає трек.${field}`);
  }

  for (const field of ['props', 'warp', 'loop', 'envelopes']) {
    assert.match(pyClip, new RegExp(`entry\\["${field}"\\]`), `bridge не кладе в кліп ${field}`);
    assert.match(jsClip, new RegExp(`out\\.${field}`), `емулятор не кладе в кліп ${field}`);
    assert.match(jsOps, new RegExp(`entry\\.${field}`), `state-ops не читає кліп.${field}`);
  }
});
