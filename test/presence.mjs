// Присутність на боці daemon: підпис виду, тротлінг і правила follow.

import assert from 'node:assert/strict';
import test from 'node:test';
import { PresenceKeeper, describePresence, shouldFollow, viewSignature } from '../daemon/presence.js';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const look = (id, extra = {}) => ({ track: { id }, names: { track: `назва ${id}` }, ...extra });

function keeper(options = {}) {
  const sent = [];
  const logs = [];
  const it = new PresenceKeeper({
    send: (m) => sent.push(m), log: (t) => logs.push(t), minIntervalMs: 40, ...options,
  });
  it.enable();
  return { it, sent, logs };
}

test('підпис не залежить від назв: перейменування не є рухом погляду', () => {
  const before = viewSignature({ track: { id: 't1' }, names: { track: 'Bass' } });
  const after = viewSignature({ track: { id: 't1' }, names: { track: 'Bass Lead' } });
  assert.equal(before, after);
  assert.notEqual(before, viewSignature({ track: { id: 't2' } }));
  assert.equal(viewSignature(null), 'none');
});

test('Return не плутається зі звичайним треком того самого id', () => {
  assert.notEqual(
    viewSignature({ track: { id: 't1' } }),
    viewSignature({ track: { id: 't1', kind: 'return' } }));
});

test('той самий вид не шлеться двічі', () => {
  const { it, sent } = keeper();
  assert.equal(it.update(look('t1')), true);
  assert.equal(it.update(look('t1')), false);
  assert.equal(it.update({ track: { id: 't1' }, names: { track: 'інша назва' } }), false);
  assert.equal(sent.length, 1);
});

test('тротлінг віддає останній вид, а не перший', async () => {
  const { it, sent } = keeper();
  for (let i = 0; i < 8; i += 1) {
    it.update(look(`t${i}`));
    await sleep(5);
  }
  assert.ok(sent.length < 8, `надіслано ${sent.length} із 8 — тротлінг не спрацював`);
  await sleep(80);
  assert.equal(viewSignature(sent.at(-1).view), viewSignature(look('t7')),
    'останнє повідомлення має нести останній вид');
});

test('поки relay не показав підтримку, не шлемо нічого', () => {
  const sent = [];
  const it = new PresenceKeeper({ send: (m) => sent.push(m), log: () => {} });
  assert.equal(it.update(look('t1')), false);
  it.enable();
  assert.equal(it.update(look('t1')), true);
  it.disable();
  assert.equal(it.update(look('t2')), false);
  assert.equal(sent.length, 1);
});

test('clear шле порожній вид рівно раз', () => {
  const { it, sent } = keeper();
  it.update(look('t1'));
  assert.equal(it.clear(), true);
  assert.equal(it.clear(), false);
  assert.deepEqual(sent.at(-1), { m: 'presence', view: null });
});

test('після розриву вид шлеться заново, навіть якщо не змінився', () => {
  const { it, sent } = keeper();
  it.update(look('t1'));
  assert.equal(sent.length, 1);
  it.reset();
  assert.equal(sent.length, 1, 'reset нічого не шле — relay нас уже забув');
  it.update(look('t1'), Date.now() + 1000);
  assert.equal(sent.length, 2);
});

test('вид того, за ким слідую, не відбивається йому назад', () => {
  const { it, sent } = keeper();
  it.onPresence([{ author: 'p2', view: look('t9') }], 'p1');
  it.following = 'p2';
  assert.equal(it.update(look('t9')), false, 'це його ж вид, який я щойно повторив');
  assert.equal(sent.length, 0);
});

test('чужі погляди логуються лише коли змінились, свій не показується', () => {
  const { it, logs } = keeper();
  const list = [
    { author: 'p2', view: { track: { id: 't1' }, names: { track: 'Bass' } } },
    { author: 'me', view: look('t5') },
  ];
  it.onPresence(list, 'me');
  it.onPresence(list, 'me');
  assert.deepEqual(logs, ['дивляться: p2: Bass']);

  it.onPresence([], 'me');
  assert.equal(logs.at(-1), 'усі дивляться кудись інде');
});

test('опис показує, хто за ким слідує', () => {
  const line = describePresence([
    { author: 'p2', view: { track: { id: 't1' }, names: { track: 'Bass', scene: 'Drop' }, scene: { id: 's1' } }, following: 'p1' },
  ], 'p1');
  assert.equal(line, 'p2: Bass / Drop (слідує за p1)');
});

test('follow відмовляє на взаємність, паузу і порожній вид', () => {
  const list = [
    { author: 'p2', view: look('t1'), following: null },
    { author: 'p3', view: look('t2'), following: 'me' },
    { author: 'p4', view: null, following: null },
  ];
  assert.equal(shouldFollow({ list, me: 'me', target: 'p2' }).ok, true);

  const mutual = shouldFollow({ list, me: 'me', target: 'p3' });
  assert.equal(mutual.ok, false);
  assert.match(mutual.reason, /уже слідує за тобою/);

  assert.equal(shouldFollow({ list, me: 'me', target: 'p4' }).ok, false);
  assert.match(shouldFollow({ list, me: 'me', target: 'p9' }).reason, /не в сесії/);

  const paused = shouldFollow({ list, me: 'me', target: 'p2', pausedUntil: Date.now() + 5000 });
  assert.equal(paused.ok, false);
  assert.equal(paused.reason, null, 'пауза мовчазна: це не помилка, а моя ж дія');
});
