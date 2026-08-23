// Локи на боці daemon: що вважається жестом і коли лок відпускається.

import assert from 'node:assert/strict';
import test from 'node:test';
import { LockKeeper, lockTarget } from '../daemon/locks.js';

const registry = {
  tracks: [{ id: 't1', idx: 0, name: 'Bass' }],
  scenes: [{ id: 's1', idx: 0, name: 'Drop' }],
  aux_tracks: [{ id: 'r1', kind: 'return', idx: 0, name: 'Reverb' }],
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test('обʼєкт лока: неперервний жест адресується, дискретна дія -- ні', () => {
  assert.deepEqual(lockTarget('TempoSet', { bpm: 128 }, registry),
    { object: 'song:tempo', label: 'темп' });
  assert.deepEqual(lockTarget('MixerSet', { track: { id: 't1' }, param: 'volume' }, registry),
    { object: 'track:t1', label: 'Bass' });
  assert.deepEqual(lockTarget('DeviceParamSet', { track: { id: 'r1', kind: 'return' } }, registry),
    { object: 'return:r1', label: 'Reverb' });
  assert.deepEqual(lockTarget('ClipNotesSet', { track: { id: 't1' }, scene: { id: 's1' } }, registry),
    { object: 'clip:t1:s1', label: 'Bass / Drop' });

  for (const type of ['ClipLaunch', 'TrackToggle', 'ObjectMetaSet', 'TrackCreate', 'RegistryInit']) {
    assert.equal(lockTarget(type, { track: { id: 't1' } }, registry), null, type);
  }
});

test('Return не плутається зі звичайним треком і без реєстру лок усе одно береться', () => {
  const aux = lockTarget('MixerSet', { track: { id: 't1', kind: 'return' } }, registry);
  assert.equal(aux.object, 'return:t1');
  assert.equal(aux.label, null, 'назви для такого aux у реєстрі немає');

  const bare = lockTarget('MixerSet', { track: { id: 't1' } }, null);
  assert.deepEqual(bare, { object: 'track:t1', label: null });
});

test('жест бере лок один раз і відпускає його після паузи', async () => {
  const sent = [];
  const keeper = new LockKeeper({ send: (m) => sent.push(m), log: () => {}, idleMs: 60 });

  for (let i = 0; i < 5; i += 1) {
    keeper.touch('MixerSet', { track: { id: 't1' }, param: 'volume', value: i / 5 }, registry);
    await sleep(10);
  }
  assert.deepEqual(sent.filter((m) => m.m === 'lock').length, 1, 'один лок на весь жест');
  assert.equal(sent[0].label, 'Bass');
  assert.equal(sent.some((m) => m.m === 'unlock'), false, 'жест ще триває');

  await sleep(120);
  assert.deepEqual(sent.at(-1), { m: 'unlock', object: 'track:t1' });

  keeper.touch('MixerSet', { track: { id: 't1' }, param: 'volume', value: 1 }, registry);
  assert.equal(sent.filter((m) => m.m === 'lock').length, 2, 'новий жест -- новий лок');
});

test('різні обʼєкти тримаються паралельно', async () => {
  const sent = [];
  const keeper = new LockKeeper({ send: (m) => sent.push(m), log: () => {}, idleMs: 60 });
  keeper.touch('MixerSet', { track: { id: 't1' } }, registry);
  keeper.touch('ClipNotesSet', { track: { id: 't1' }, scene: { id: 's1' } }, registry);
  assert.deepEqual(sent.map((m) => m.object), ['track:t1', 'clip:t1:s1']);

  await sleep(120);
  assert.deepEqual(sent.filter((m) => m.m === 'unlock').map((m) => m.object).sort(),
    ['clip:t1:s1', 'track:t1']);
});

test('розрив звʼязку не шле unlock у нікуди', async () => {
  const sent = [];
  const keeper = new LockKeeper({ send: (m) => sent.push(m), log: () => {}, idleMs: 40 });
  keeper.touch('TempoSet', { bpm: 130 }, registry);
  keeper.reset();
  await sleep(80);
  assert.equal(sent.filter((m) => m.m === 'unlock').length, 0, 'локи знімає relay при розриві');
});

test('чужі локи логуються лише коли змінились', () => {
  const lines = [];
  const keeper = new LockKeeper({ send: () => {}, log: (t) => lines.push(t) });
  const locks = (list) => keeper.onLocks(list, 'me');

  locks([{ object: 'track:t1', label: 'Bass', author: 'p2' }, { object: 'song:tempo', author: 'me' }]);
  locks([{ object: 'track:t1', label: 'Bass', author: 'p2' }]);
  assert.deepEqual(lines, ['редагують: p2 — Bass'], 'власний лок не показуємо, повтор не дублюємо');

  locks([]);
  assert.deepEqual(lines.at(-1), 'ніхто нічого не редагує');
});

test('петля і ноти того самого кліпу діляться одним локом', () => {
  const payload = { track: { id: 't1' }, scene: { id: 's1' } };
  const notes = lockTarget('ClipNotesSet', payload, registry);
  const loop = lockTarget('ClipLoopSet', payload, registry);
  assert.equal(loop.object, notes.object, 'редагування нот і петлі -- один обʼєкт');
  assert.equal(loop.object, 'clip:t1:s1');
});
