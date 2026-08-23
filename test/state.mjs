// Збірка повного стану з чанків: UDP губить і переставляє, знімок має або
// зібратись цілим, або не зʼявитись узагалі.

import assert from 'node:assert/strict';
import test from 'node:test';
import { StateCollector, stateDigest, summarize } from '../daemon/state.js';
import { noteRegionsFor, stateToOps } from '../daemon/tools/state-ops.js';

const blobOf = (state) => JSON.stringify(state);
const chunksOf = (state, id, size = 10) => {
  const blob = blobOf(state);
  const parts = [];
  for (let i = 0; i < blob.length; i += size) parts.push(blob.slice(i, i + size));
  return parts.map((data, seq) => ({
    m: 'state_chunk', id, seq, total: parts.length, chars: blob.length, data,
  }));
};

const sample = {
  version: 1,
  tempo: 128,
  tracks: [{
    id: 't1',
    devices: [{ device: { class_name: 'Operator', ordinal: 0 }, parameters: [{ name: 'A', ordinal: 0, value: 0.5 }] }],
    clips: [{ scene: { id: 's1' }, notes: [{ pitch: 60 }, { pitch: 64 }] }],
  }],
  aux_tracks: [{ id: 'r1', kind: 'return', devices: [], clips: [] }],
  scenes: [{ id: 's1' }],
};

function collector(extra = {}) {
  const done = [];
  const logs = [];
  const it = new StateCollector({ log: (t) => logs.push(t), onComplete: (s, i) => done.push([s, i]), ...extra });
  return { it, done, logs };
}

test('знімок зʼявляється лише коли зібрані всі чанки', () => {
  const { it, done } = collector();
  const chunks = chunksOf(sample, 7);
  assert.ok(chunks.length > 3, 'потрібно кілька чанків');

  for (const chunk of chunks.slice(0, -1)) assert.equal(it.chunk(chunk), 'partial');
  assert.deepEqual(done, []);
  assert.deepEqual(it.pending(), { id: 7, got: chunks.length - 1, total: chunks.length });

  assert.equal(it.chunk(chunks.at(-1)), 'complete');
  assert.deepEqual(done[0][0], sample);
  assert.equal(it.pending(), null);
});

test('порядок чанків не має значення', () => {
  const { it, done } = collector();
  const chunks = chunksOf(sample, 8);
  for (const chunk of [...chunks].reverse()) it.chunk(chunk);
  assert.deepEqual(done[0][0], sample);
});

test('новий знімок скасовує напівзібраний старий', () => {
  const { it, done, logs } = collector();
  const older = chunksOf(sample, 1);
  it.chunk(older[0]);
  const newer = chunksOf({ ...sample, tempo: 130 }, 2);
  for (const chunk of newer) it.chunk(chunk);

  assert.equal(done.length, 1);
  assert.equal(done[0][0].tempo, 130);
  assert.match(logs.join(' '), /лишився без \d+ чанків/);

  // Спізнілий чанк старого знімка не має воскрешати його
  assert.equal(it.chunk(older[1]), 'partial');
  assert.equal(done.length, 1);
});

test('протухлий знімок не змішується з наступним', async () => {
  const { it, logs } = collector({ ttlMs: 30 });
  const chunks = chunksOf(sample, 3);
  it.chunk(chunks[0]);
  await new Promise((resolve) => setTimeout(resolve, 60));
  it.chunk(chunks[1]);
  assert.match(logs.join(' '), /протух/);
  assert.equal(it.pending().got, 1);
});

test('битий і неповний знімок не потрапляє назовні', () => {
  const { it, done, logs } = collector();
  assert.equal(it.chunk({ id: 1, seq: 0, total: 1, data: 'не json' }), 'broken');
  assert.deepEqual(done, []);
  assert.match(logs.join(' '), /не парситься/);

  assert.equal(it.chunk({ id: 2, seq: 5, total: 2, data: '{}' }), 'ignored', 'seq поза межами');
  assert.equal(it.chunk({ id: 2, seq: 0, total: 0, data: '{}' }), 'ignored', 'total нуль');
  assert.equal(it.chunk({ id: 2, seq: 0, total: 1, data: 42 }), 'ignored', 'data не рядок');

  const short = { id: 3, seq: 0, total: 1, chars: 999, data: '{}' };
  assert.equal(it.chunk(short), 'broken', 'довжина не збіглась із обіцяною');
  assert.deepEqual(done, []);
});

test('підсумок рахує девайси, параметри й ноти по всіх треках', () => {
  assert.deepEqual(summarize(sample), {
    tracks: 1, aux_tracks: 1, scenes: 1, devices: 1, parameters: 1, clips: 1, notes: 2,
  });
  assert.deepEqual(summarize({}), {
    tracks: 0, aux_tracks: 0, scenes: 0, devices: 0, parameters: 0, clips: 0, notes: 0,
  });
});

test('digest порівнює вміст, а не момент зняття', () => {
  assert.equal(stateDigest(sample), stateDigest(JSON.parse(blobOf(sample))));
  assert.notEqual(stateDigest(sample), stateDigest({ ...sample, tempo: 129 }));

  // Два сети однакові, але зняті різними машинами в різний час
  const mine = { ...sample, at: 1, script: '0.18.0', live: '12.3.8' };
  const theirs = { ...sample, at: 999, script: '0.18.0-fake', live: '12.3.5' };
  assert.equal(stateDigest(mine), stateDigest(theirs));

  // Порядок ключів у JSON з Python і з JS різний, digest -- ні
  const reordered = { scenes: sample.scenes, tracks: sample.tracks, aux_tracks: sample.aux_tracks, tempo: sample.tempo, version: 1 };
  assert.equal(stateDigest(sample), stateDigest(reordered));
});

// --------------------------------------------- переклад знімка в події

test('регіон нот починається з нуля і вкриває кліп цілком', () => {
  const meta = { length: 4 };
  const [[region, notes]] = noteRegionsFor(meta, [
    { pitch: 60, start_time: 1.5 }, { pitch: 62, start_time: 0.5 },
  ]);
  assert.equal(region.from_time, 0, 'регіон має чистити слот від нуля');
  assert.equal(region.from_pitch, 0);
  assert.equal(region.pitch_span, 128);
  assert.ok(region.time_span >= 4);
  assert.deepEqual(notes.map((n) => n.start_time), [0.5, 1.5], 'ноти впорядковані');
});

test('порожній кліп дає регіон без нот -- він чистить чужі', () => {
  const [[region, notes]] = noteRegionsFor({ length: 8 }, []);
  assert.deepEqual(notes, []);
  assert.equal(region.from_time, 0);
  assert.equal(region.time_span, 8);
});

test('великий кліп ріжеться на регіони, що стикуються без дірок', () => {
  const notes = Array.from({ length: 2500 }, (_, i) => ({ pitch: 60, start_time: i * 0.25 }));
  const regions = noteRegionsFor({ length: 4 }, notes);
  assert.ok(regions.length > 1, 'понад 1024 ноти мають розкластись на кілька регіонів');

  let cursor = 0;
  let seen = 0;
  for (const [region, part] of regions) {
    assert.equal(region.from_time, cursor, 'регіони мають стикуватись впритул');
    assert.ok(part.length <= 1024, `у регіоні ${part.length} нот`);
    for (const note of part) {
      assert.ok(note.start_time >= region.from_time
        && note.start_time < region.from_time + region.time_span, 'нота поза своїм регіоном');
    }
    seen += part.length;
    cursor = region.from_time + region.time_span;
  }
  assert.equal(seen, notes.length, 'жодна нота не загубилась');
  assert.ok(cursor > notes.at(-1).start_time, 'останній регіон має вкривати останню ноту');
});

test('знімок розкладається на події з тими самими адресами', () => {
  const ops = stateToOps({
    tempo: 128,
    tracks: [{
      id: 't1', name: 'Bass',
      mixer: { volume: 0.5, sends: [{ index: 0, value: 0.25 }], mute: true },
      devices: [{
        chain_path: [{ id: 'c1' }],
        device: { class_name: 'Compressor2', class_display_name: 'Compressor', ordinal: 0 },
        parameters: [{ name: 'Threshold', ordinal: 0, value: 0.7 }],
      }],
      clips: [{ scene: { id: 's1' }, clip: { length: 4, name: 'Loop' }, notes: [{ pitch: 60, start_time: 0 }] }],
    }],
    aux_tracks: [{ id: 'r1', kind: 'return', mixer: { volume: 0.4 } }],
    scenes: [{ id: 's1', name: 'Intro' }],
  });
  const types = ops.map(([type]) => type);
  assert.deepEqual(types[0], 'TempoSet');
  assert.ok(types.indexOf('ClipCreate') < types.indexOf('ClipNotesSet'), 'кліп створюється до нот');

  const device = ops.find(([type]) => type === 'DeviceParamSet')[1];
  assert.deepEqual(device.chain_path, [{ id: 'c1' }]);
  assert.equal(device.parameter.name, 'Threshold');

  const aux = ops.find(([type, p]) => type === 'MixerSet' && p.track.kind === 'return')[1];
  assert.deepEqual(aux.track, { id: 'r1', kind: 'return' });

  const toggle = ops.find(([type]) => type === 'TrackToggle')[1];
  assert.equal(toggle.param, 'mute');
  assert.equal(toggle.value, true);

  const scene = ops.find(([type, p]) => type === 'ObjectMetaSet' && p.object === 'scene')[1];
  assert.deepEqual(scene.scene, { id: 's1' });
});

test('обʼєкт без uuid у події не перетворюється', () => {
  const ops = stateToOps({ tracks: [{ name: 'без id' }], aux_tracks: [{ id: 'r1' }], scenes: [{}] });
  assert.deepEqual(ops, []);
});

// Живий Live віддає 63072000 (два роки в секундах) для кліпу під запис.
// Знімок, зроблений старим скриптом, донесе це число сюди -- регіон нот
// такої довжини Live не переварить.
test('отруєна довжина кліпу не породжує регіон завдовжки два роки', () => {
  const [[region]] = noteRegionsFor({ length: 63072000 }, [{ pitch: 60, start_time: 0 }]);
  assert.ok(region.time_span < 1e6, `регіон роздувся до ${region.time_span}`);

  const [[sane]] = noteRegionsFor({ length: 8 }, [{ pitch: 60, start_time: 0 }]);
  assert.equal(sane.time_span, 8, 'притомна довжина лишається як є');

  const [[inf]] = noteRegionsFor({ length: Infinity }, []);
  assert.ok(Number.isFinite(inf.time_span));
});

test('межі кліпу виставляються після створення і після нот', () => {
  const ops = stateToOps({
    tracks: [{
      id: 't1',
      clips: [{
        scene: { id: 's1' },
        clip: { length: 4 },
        notes: [{ pitch: 60, start_time: 0 }],
        loop: { looping: true, loop_start: 0, loop_end: 4, start_marker: 0, end_marker: 4 },
      }],
    }],
  });
  const types = ops.map(([type]) => type);
  const loop = types.indexOf('ClipLoopSet');
  assert.ok(loop > types.indexOf('ClipCreate'), 'петля після створення кліпу');
  assert.ok(loop > types.lastIndexOf('ClipNotesSet'), 'петля після нот');
  assert.deepEqual(ops[loop][1].track, { id: 't1' });
  assert.equal(ops[loop][1].loop_end, 4);
});

test('кліп без петлі не породжує ClipLoopSet', () => {
  const ops = stateToOps({ tracks: [{ id: 't1', clips: [{ scene: { id: 's1' }, notes: [] }] }] });
  assert.ok(!ops.some(([type]) => type === 'ClipLoopSet'));
});
