// Стиснення хвоста журналу: що згортається, а що не сміє згорнутись ніколи.

import assert from 'node:assert/strict';
import test from 'node:test';
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
