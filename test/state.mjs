// Збірка повного стану з чанків: UDP губить і переставляє, знімок має або
// зібратись цілим, або не зʼявитись узагалі.

import assert from 'node:assert/strict';
import test from 'node:test';
import { StateCollector, stateDigest, summarize } from '../daemon/state.js';

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
