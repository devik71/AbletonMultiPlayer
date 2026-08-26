// Стиснення хвоста журналу: що згортається, а що не сміє згорнутись ніколи.

import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { compactTail } from '../relay/compact.js';

let gseq = 0;
const ev = (type, payload = {}) => ({ gseq: (gseq += 1), type, payload, author: 'p1', lseq: gseq });
const types = (result) => result.events.map((e) => e.type);
const gseqs = (result) => result.events.map((e) => e.gseq);

const device = (id, ordinal, value, extra = {}) => ev('DeviceParamSet', {
  track: { id, ...extra },
  device: { class_name: 'Compressor2', class_display_name: 'Compressor', ordinal: 0 },
  parameter: { name: 'Threshold', ordinal },
  value,
});

test('остання подія хвоста лишається завжди', () => {
  gseq = 0;
  const tail = [device('t1', 0, 0.1), device('t1', 0, 0.2), device('t1', 0, 0.3)];
  const out = compactTail(tail);
  assert.equal(out.events.length, 1);
  assert.equal(out.dropped, 2);
  assert.equal(out.events[0].payload.value, 0.3);
  assert.equal(out.events[0].gseq, tail.at(-1).gseq);
});

test('стиснення не синтезує подій і не міняє порядок', () => {
  gseq = 0;
  const tail = [
    ev('RegistryInit', { tracks: [] }),
    ev('TempoSet', { bpm: 120 }),
    ev('TrackCreate', { track: { id: 't2' }, idx: 1, kind: 'midi' }),
    ev('TempoSet', { bpm: 128 }),
  ];
  const out = compactTail(tail);
  assert.deepEqual(types(out), ['RegistryInit', 'TrackCreate', 'TempoSet']);
  assert.deepEqual(gseqs(out), [1, 3, 4]);
  for (const kept of out.events) assert.ok(tail.includes(kept), 'подія має бути тим самим обʼєктом');
});

test('адреса параметра розрізняє трек, параметр і вкладений chain', () => {
  gseq = 0;
  const nested = ev('DeviceParamSet', {
    track: { id: 't1' },
    chain_path: [{ id: 'c1' }],
    device: { class_name: 'Compressor2', class_display_name: 'Compressor', ordinal: 0 },
    parameter: { name: 'Threshold', ordinal: 0 },
    value: 0.9,
  });
  const out = compactTail([
    device('t1', 0, 0.1), device('t2', 0, 0.2), device('t1', 1, 0.3), nested,
  ]);
  assert.equal(out.dropped, 0, 'різні адреси не згортаються');

  gseq = 0;
  const same = compactTail([device('t1', 0, 0.1), device('t1', 0, 0.9)]);
  assert.equal(same.events.length, 1);
});

test('Return і Master не згортаються у звичайний трек', () => {
  gseq = 0;
  const out = compactTail([
    device('t1', 0, 0.1, {}),
    device('t1', 0, 0.2, { kind: 'return' }),
    device('t1', 0, 0.3, { kind: 'master' }),
  ]);
  assert.equal(out.dropped, 0);
});

test('MixerSet: send розрізняється індексом, toggle -- параметром', () => {
  gseq = 0;
  const mixer = (param, value, index) => ev('MixerSet', { track: { id: 't1' }, param, index, value });
  const out = compactTail([
    mixer('send', 0.1, 0), mixer('send', 0.2, 1), mixer('volume', 0.3), mixer('send', 0.4, 0),
  ]);
  assert.equal(out.events.length, 3);
  assert.deepEqual(out.events.map((e) => e.payload.value), [0.2, 0.3, 0.4]);

  gseq = 0;
  const toggles = compactTail([
    ev('TrackToggle', { track: { id: 't1' }, param: 'mute', value: true }),
    ev('TrackToggle', { track: { id: 't1' }, param: 'solo', value: true }),
    ev('TrackToggle', { track: { id: 't1' }, param: 'mute', value: false }),
  ]);
  assert.equal(toggles.events.length, 2);
  assert.deepEqual(toggles.events.map((e) => e.payload.param), ['solo', 'mute']);
});

test('ClipNotesSet згортається лише в межах того самого регіону', () => {
  gseq = 0;
  const notes = (from_time, count) => ev('ClipNotesSet', {
    track: { id: 't1' },
    scene: { id: 's1' },
    region: { from_time, time_span: 4, from_pitch: 60, pitch_span: 16 },
    notes: new Array(count).fill({ pitch: 60 }),
  });
  const out = compactTail([notes(0, 1), notes(4, 2), notes(0, 3)]);
  assert.equal(out.events.length, 2);
  assert.deepEqual(out.events.map((e) => e.payload.region.from_time), [4, 0]);
  assert.equal(out.events.at(-1).payload.notes.length, 3);
});

test('SceneLaunch і StopAllClips перекривають усе, що запускалось раніше', () => {
  gseq = 0;
  const launch = (id) => ev('ClipLaunch', { track: { id }, scene: { id: 's1' } });
  const out = compactTail([
    launch('t1'), launch('t2'), ev('SceneLaunch', { scene: { id: 's2' } }), launch('t1'),
  ]);
  assert.deepEqual(types(out), ['SceneLaunch', 'ClipLaunch']);
  assert.equal(out.events.at(-1).payload.track.id, 't1');

  gseq = 0;
  const stopped = compactTail([launch('t1'), launch('t2'), ev('StopAllClips', {})]);
  assert.deepEqual(types(stopped), ['StopAllClips']);

  gseq = 0;
  const perTrack = compactTail([
    launch('t1'), ev('ClipStop', { track: { id: 't1' } }), launch('t2'),
  ]);
  assert.deepEqual(types(perTrack), ['ClipStop', 'ClipLaunch']);
});

test('структурні й незнайомі події не згортаються ніколи', () => {
  gseq = 0;
  const out = compactTail([
    ev('TrackCreate', { track: { id: 't1' }, idx: 0, kind: 'midi' }),
    ev('ClipCreate', { track: { id: 't1' }, scene: { id: 's1' }, clip: { length: 4 } }),
    ev('ClipDelete', { track: { id: 't1' }, scene: { id: 's1' } }),
    ev('TrackDelete', { track: { id: 't1' } }),
    ev('SceneCreate', { scene: { id: 's2' }, idx: 0 }),
    ev('SceneDelete', { scene: { id: 's2' } }),
  ]);
  assert.equal(out.dropped, 0);

  gseq = 0;
  // Новий bridge шле тип, якого цей relay не знає: викидати його не можна.
  const unknown = compactTail([
    ev('SomethingFromTheFuture', { clip: { id: 'a1' }, at: 1 }),
    ev('SomethingFromTheFuture', { clip: { id: 'a1' }, at: 2 }),
  ]);
  assert.equal(unknown.dropped, 0);
});

test('порожній хвіст не ламає стиснення', () => {
  const out = compactTail([]);
  assert.deepEqual(out.events, []);
  assert.equal(out.dropped, 0);
});

test('межі кліпу згортаються в останні, і лише в межах свого кліпу', () => {
  gseq = 0;
  const loop = (track, scene, end) => ev('ClipLoopSet', {
    track: { id: track }, scene: { id: scene },
    looping: true, loop_start: 0, loop_end: end, start_marker: 0, end_marker: end,
  });
  // Живий Live підтвердив: тягнення брекета дає серію змін на одну адресу
  const out = compactTail([loop('t1', 's1', 6), loop('t1', 's1', 7), loop('t1', 's1', 8)]);
  assert.equal(out.events.length, 1);
  assert.equal(out.events[0].payload.loop_end, 8);

  gseq = 0;
  const two = compactTail([loop('t1', 's1', 4), loop('t1', 's2', 4)]);
  assert.equal(two.dropped, 0, 'різні кліпи не перекривають одне одного');
});

test('переїзд кліпу в Arrangement згортається, а створення -- ні', () => {
  gseq = 0;
  const move = (start) => ev('ArrangementClipMove',
    { track: { id: 't1' }, clip: { id: 'a1' }, start_time: start });
  const out = compactTail([move(8), move(16), move(24)]);
  assert.equal(out.events.length, 1);
  assert.equal(out.events[0].payload.start_time, 24);

  gseq = 0;
  // Різні кліпи не перекривають одне одного
  const two = compactTail([move(8), ev('ArrangementClipMove',
    { track: { id: 't1' }, clip: { id: 'a2' }, start_time: 8 })]);
  assert.equal(two.dropped, 0);

  gseq = 0;
  // Створення і видалення -- структура, вона не згортається ніколи
  const structural = compactTail([
    ev('ArrangementClipCreate', { track: { id: 't1' }, clip: { id: 'a1' }, start_time: 0 }),
    ev('ArrangementClipDelete', { track: { id: 't1' }, clip: { id: 'a1' } }),
  ]);
  assert.equal(structural.dropped, 0);
});

test('властивості пісні згортаються кожна у своїй адресі', () => {
  gseq = 0;
  const set = (prop, value) => ev('SongPropSet', { prop, value });
  const out = compactTail([set('signature_numerator', 3), set('signature_numerator', 6),
    set('signature_numerator', 7)]);
  assert.equal(out.events.length, 1);
  assert.equal(out.events[0].payload.value, 7);

  gseq = 0;
  // Розмір такту не перекриває тональність: різні властивості -- різні адреси
  const mixed = compactTail([set('signature_numerator', 3), set('root_note', 5)]);
  assert.equal(mixed.dropped, 0);
});

test('перевизначення сцени згортається в останнє, і лише в межах своєї сцени', () => {
  gseq = 0;
  const timing = (scene, tempo) => ev('SceneTimingSet', {
    scene: { id: scene }, tempo_enabled: true, tempo,
    time_signature_enabled: false,
  });
  const out = compactTail([timing('s1', 120), timing('s1', 130), timing('s1', 140)]);
  assert.equal(out.events.length, 1);
  assert.equal(out.events[0].payload.tempo, 140);

  gseq = 0;
  assert.equal(compactTail([timing('s1', 120), timing('s2', 120)]).dropped, 0);
});

test('властивість кліпу згортається у своїй адресі, не зачіпаючи сусідні', () => {
  gseq = 0;
  const set = (prop, value) => ev('ClipPropSet', {
    track: { id: 't1' }, scene: { id: 's1' }, prop, value,
  });
  const out = compactTail([set('gain', 0.2), set('gain', 0.5), set('gain', 0.9)]);
  assert.equal(out.events.length, 1);
  assert.equal(out.events[0].payload.value, 0.9);

  gseq = 0;
  // gain не перекриває warp_mode, і кліпи не перекривають один одного
  assert.equal(compactTail([set('gain', 0.2), set('warp_mode', 3)]).dropped, 0);
  gseq = 0;
  const other = ev('ClipPropSet', { track: { id: 't1' }, scene: { id: 's2' }, prop: 'gain', value: 0.2 });
  assert.equal(compactTail([set('gain', 0.2), other]).dropped, 0);
});

test('локатор адресується часом, і згортання коректне в обидва боки', () => {
  gseq = 0;
  const set = (time, name) => ev('CueSet', { time, name });
  const del = (time) => ev('CueDelete', { time });

  // перейменування згортається в останнє
  let out = compactTail([set(32, 'a'), set(32, 'b'), set(32, 'Drop')]);
  assert.equal(out.events.length, 1);
  assert.equal(out.events[0].payload.name, 'Drop');

  // створення + видалення = видалення; кожна подія повністю визначає стан
  gseq = 0;
  out = compactTail([set(32, 'Drop'), del(32)]);
  assert.equal(out.events.length, 1);
  assert.equal(out.events[0].type, 'CueDelete');

  // і навпаки
  gseq = 0;
  out = compactTail([del(32), set(32, 'Drop')]);
  assert.equal(out.events.length, 1);
  assert.equal(out.events[0].type, 'CueSet');

  // різні позиції -- різні адреси
  gseq = 0;
  assert.equal(compactTail([set(32, 'a'), set(64, 'b')]).dropped, 0);
});

test('warp-маркери згортаються в останній набір, кожен кліп окремо', () => {
  gseq = 0;
  const warp = (scene, markers) => ev('ClipWarpSet', {
    track: { id: 't1' }, scene: { id: scene }, markers,
  });
  const out = compactTail([
    warp('s1', [{ beat_time: 0, sample_time: 0 }]),
    warp('s1', [{ beat_time: 0, sample_time: 0 }, { beat_time: 4, sample_time: 2 }]),
    warp('s1', [{ beat_time: 0, sample_time: 0 }, { beat_time: 8, sample_time: 4 }]),
  ]);
  assert.equal(out.events.length, 1);
  assert.equal(out.events[0].payload.markers[1].beat_time, 8);

  gseq = 0;
  assert.equal(compactTail([warp('s1', [{ beat_time: 0, sample_time: 0 }]),
    warp('s2', [{ beat_time: 0, sample_time: 0 }])]).dropped, 0);
});

test('кліп у лінійці має власну адресу згортання, окрему від сесійної', () => {
  gseq = 0;
  const inSlot = (v) => ev('ClipPropSet', {
    track: { id: 't1' }, scene: { id: 's1' }, prop: 'gain', value: v,
  });
  const inArr = (v) => ev('ClipPropSet', {
    track: { id: 't1' }, clip: { id: 'a1' }, prop: 'gain', value: v,
  });
  // той самий трек і та сама властивість, але різні кліпи -- не перекривають
  assert.equal(compactTail([inSlot(0.2), inArr(0.8)]).dropped, 0);

  gseq = 0;
  const out = compactTail([inArr(0.2), inArr(0.5), inArr(0.9)]);
  assert.equal(out.events.length, 1);
  assert.equal(out.events[0].payload.value, 0.9);

  gseq = 0;
  const loopArr = (end) => ev('ClipLoopSet', {
    track: { id: 't1' }, clip: { id: 'a1' }, looping: true, loop_start: 0, loop_end: end,
  });
  assert.equal(compactTail([loopArr(4), loopArr(8)]).events.length, 1);
});

test('у switch стиснення немає двох гілок на один тип', () => {
  // Дубльований case в JS -- мертвий код: друга гілка недосяжна назавжди.
  // Тут це особливо підступно, бо дубль виглядає як робоче правило згортання
  // і читач вважає, що воно діє. SongPropSet прожив так цілий реліз.
  const src = readFileSync(new URL('../relay/compact.js', import.meta.url), 'utf8');
  const labels = [...src.matchAll(/^\s*case '([A-Za-z]+)':/gm)].map((m) => m[1]);
  const twice = labels.filter((t, i) => labels.indexOf(t) !== i);
  assert.deepEqual(twice, [], `тип згортається двічі: ${twice.join(', ')}`);
});

test('кожен тип, який bridge уміє застосувати, має рішення про згортання', () => {
  // Рішення може бути й «не згортати» -- воно тоді записане в коментарі
  // до default. Чого не має бути, так це типу, про який ніхто не думав.
  const src = readFileSync(new URL('../relay/compact.js', import.meta.url), 'utf8');
  const bridge = readFileSync(
    new URL('../remote-script/AbletonMP/AbletonMP.py', import.meta.url), 'utf8');
  const block = bridge.slice(bridge.indexOf('APPLY_TYPES'), bridge.indexOf('CLIP_PROPS'));
  const applied = new Set([...block.matchAll(/"([A-Z][A-Za-z]+)"/g)].map((m) => m[1]));
  assert.ok(applied.size >= 20, `APPLY_TYPES прочитано не повністю: ${applied.size}`);
  // Явно перелічені або свідомо лишені структурними -- обидва варіанти ок,
  // тест лише ловить тип, доданий у bridge і забутий у relay.
  const folded = new Set([...src.matchAll(/^\s*case '([A-Za-z]+)':/gm)].map((m) => m[1]));
  const structural = new Set([...src.matchAll(/NEVER_FOLD:\s*([A-Za-z, ]+)/g)]
    .flatMap((m) => m[1].split(',').map((s) => s.trim())).filter(Boolean));
  const forgotten = [...applied].filter((t) => !folded.has(t) && !structural.has(t)).sort();
  assert.deepEqual(forgotten, [],
    `тип є в APPLY_TYPES, але relay про нього не знає: ${forgotten.join(', ')}`);
});
