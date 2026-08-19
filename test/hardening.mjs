import assert from 'node:assert/strict';
import { createHash, randomBytes } from 'node:crypto';
import { spawn } from 'node:child_process';
import {
  existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, utimesSync, writeFileSync,
} from 'node:fs';
import { connect, createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import WebSocket from '../daemon/node_modules/ws/index.js';
import { FileSync } from '../daemon/filesync.js';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const hash = (data) => createHash('sha256').update(data).digest('hex').slice(0, 16);
const canonical = (value) => {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  return '{' + Object.keys(value).sort()
    .map((key) => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
};

function manifestFor(messages, path) {
  return messages.filter((m) => m.m === 'files_manifest').at(-1)?.files.find((f) => f.path === path);
}

test('File Sync помічає зміну того самого розміру', () => {
  const dir = mkdtempSync(join(tmpdir(), 'abletonmp-filesync-change-'));
  try {
    const samples = join(dir, 'Samples');
    const file = join(samples, 'same-size.wav');
    mkdirSync(samples, { recursive: true });
    writeFileSync(file, Buffer.from('aaaa'));

    const messages = [];
    const sync = new FileSync({ send: (m) => messages.push(m), log: () => {} });
    sync.setProjectRoot(dir);
    const before = manifestFor(messages, 'Samples/same-size.wav');

    writeFileSync(file, Buffer.from('bbbb'));
    const changedAt = new Date(Date.now() + 2000);
    utimesSync(file, changedAt, changedAt);
    sync.rescan();
    const after = manifestFor(messages, 'Samples/same-size.wav');

    assert.equal(before.size, after.size);
    assert.notEqual(before.hash, after.hash);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('File Sync відхиляє traversal і перевіряє вміст перед атомарним записом', () => {
  const dir = mkdtempSync(join(tmpdir(), 'abletonmp-filesync-receive-'));
  try {
    mkdirSync(join(dir, 'Samples'), { recursive: true });
    const messages = [];
    const logs = [];
    const sync = new FileSync({ send: (m) => messages.push(m), log: (...a) => logs.push(a.join(' ')) });
    sync.setProjectRoot(dir);

    const good = Buffer.from('correct sample bytes');
    const expected = { path: 'Samples/good.wav', size: good.length, hash: hash(good) };
    sync.onManifest([
      { path: '../escape.wav', size: 4, hash: hash(Buffer.from('evil')) },
      expected,
    ]);
    const requests = messages.filter((m) => m.m === 'file_request');
    assert.deepEqual(requests.map((m) => m.path), ['Samples/good.wav']);

    const corrupt = Buffer.alloc(good.length, 0x78);
    sync.onChunk({ path: expected.path, seq: 0, total: 1, data: corrupt.toString('base64') });
    assert.equal(existsSync(join(dir, 'Samples', 'good.wav')), false);
    assert.match(logs.join('\n'), /hash або розмір не збігається/);

    sync.onManifest([expected]);
    sync.onChunk({ path: expected.path, seq: 0, total: 1, data: good.toString('base64') });
    assert.deepEqual(readFileSync(join(dir, 'Samples', 'good.wav')), good);
    assert.equal(readdirSync(join(dir, 'Samples')).some((name) => name.includes('.abletonmp-')), false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

function freePort() {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close((error) => error ? reject(error) : resolve(port));
    });
  });
}

function waitForOutput(proc, pattern, ms = 5000) {
  return new Promise((resolve, reject) => {
    const deadline = setTimeout(() => reject(new Error(`relay не вивів ${pattern}\n${proc.out}`)), ms);
    const inspect = () => {
      if (!pattern.test(proc.out)) return;
      clearTimeout(deadline);
      resolve();
    };
    proc.stdout.on('data', inspect);
    proc.stderr.on('data', inspect);
    inspect();
  });
}

function exchange(port, join, afterWelcome = null) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`ws://127.0.0.1:${port}`);
    const seen = [];
    const timeout = setTimeout(() => {
      ws.close();
      reject(new Error(`немає відповіді relay: ${JSON.stringify(seen)}`));
    }, 5000);
    ws.on('open', () => ws.send(JSON.stringify(join)));
    ws.on('message', (raw) => {
      const msg = JSON.parse(raw);
      seen.push(msg);
      if (msg.m === 'welcome' && afterWelcome) afterWelcome(ws);
      if (msg.m === 'error') {
        clearTimeout(timeout);
        ws.close();
        resolve({ error: msg, seen });
      }
    });
    ws.on('error', reject);
  });
}

test('relay відхиляє traversal у назві сесії та не commit-ить без журналу', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'abletonmp-relay-hardening-'));
  const port = await freePort();
  const blocked = join(dir, 'blocked.jsonl');
  mkdirSync(blocked);

  const body = {
    gseq: 1, type: 'TempoSet', payload: { bpm: 120 }, author: 'seed', lseq: 1, ts: null, srv_ts: 1,
  };
  const first = {
    ...body,
    prev_hash: '',
    hash: createHash('sha256').update(canonical(body)).digest('hex'),
  };
  writeFileSync(join(dir, 'damaged.jsonl'), `${JSON.stringify(first)}\nnot-json\n`);
  writeFileSync(join(dir, 'anchored.jsonl'), `${JSON.stringify(first)}\n`);
  writeFileSync(join(dir, 'anchored.checkpoint.json'), JSON.stringify({
    version: 1, session: 'anchored', gseq: 1, hash: '0'.repeat(64), updated_at: 1,
  }));

  const relay = spawn(process.execPath, [join(root, 'relay/server.js')], {
    cwd: join(root, 'relay'),
    env: { ...process.env, MP_RELAY_PORT: String(port), MP_JOURNAL_DIR: dir },
  });
  relay.out = '';
  relay.stdout.on('data', (b) => { relay.out += b.toString(); });
  relay.stderr.on('data', (b) => { relay.out += b.toString(); });

  try {
    await waitForOutput(relay, /relay слухає/);
    const bad = await exchange(port, { m: 'join', session: '../escape', author: 'p1', since: 0, proto: 1 });
    assert.equal(bad.error.code, 'bad_session');

    const failed = await exchange(
      port,
      { m: 'join', session: 'blocked', author: 'p1', since: 0, proto: 1 },
      (ws) => ws.send(JSON.stringify({
        m: 'submit', event: { type: 'TempoSet', payload: { bpm: 128 }, lseq: 1 },
      })),
    );
    assert.equal(failed.error.code, 'journal_write_failed');
    assert.equal(failed.seen.some((m) => m.m === 'commit'), false);

    const damaged = await exchange(
      port,
      { m: 'join', session: 'damaged', author: 'p2', since: 0, proto: 1 },
      (ws) => ws.send(JSON.stringify({
        m: 'submit', event: { type: 'TempoSet', payload: { bpm: 130 }, lseq: 1 },
      })),
    );
    assert.equal(damaged.error.code, 'journal_write_failed');
    assert.equal(damaged.seen.find((m) => m.m === 'welcome')?.head.gseq, 1);
    assert.equal(damaged.seen.some((m) => m.m === 'commit' && m.event.gseq === 2), false);

    const health = await fetch(`http://127.0.0.1:${port}/health`).then((r) => r.json());
    assert.match(health.sessions.find((s) => s.session === 'damaged').journal_error, /пошкоджений рядок/);

    const anchored = await exchange(
      port,
      { m: 'join', session: 'anchored', author: 'p3', since: 0, proto: 1 },
      (ws) => ws.send(JSON.stringify({
        m: 'submit', event: { type: 'TempoSet', payload: { bpm: 140 }, lseq: 1 },
      })),
    );
    assert.equal(anchored.error.code, 'journal_write_failed');
    const anchoredHealth = await fetch(`http://127.0.0.1:${port}/health`).then((r) => r.json());
    assert.match(anchoredHealth.sessions.find((s) => s.session === 'anchored').journal_error,
      /checkpoint не збігається/);
  } finally {
    relay.kill();
    rmSync(dir, { recursive: true, force: true });
  }
});

// Клієнт, який приймає, але нічого не відповідає -- саме так виглядає машина
// з вимкненим Wi-Fi: TCP-сокет живий, ws-ping лишається без pong. Справжній
// ws-клієнт для цього не годиться, бо pong він шле автоматично.
const CRLF = String.fromCharCode(13, 10);

function maskedFrame(text) {
  const payload = Buffer.from(text);
  const mask = randomBytes(4);
  const masked = Buffer.from(payload);
  for (let i = 0; i < masked.length; i += 1) masked[i] ^= mask[i % 4];
  const header = payload.length < 126
    ? Buffer.from([0x81, 0x80 | payload.length])
    : Buffer.from([0x81, 0xfe, payload.length >> 8, payload.length & 0xff]);
  return Buffer.concat([header, mask, masked]);
}

function deafClient(port, { session, author, features }) {
  return new Promise((resolve, reject) => {
    const socket = connect(port, '127.0.0.1');
    let upgraded = false;
    let head = '';
    socket.on('error', reject);
    socket.on('connect', () => socket.write([
      'GET / HTTP/1.1',
      `Host: 127.0.0.1:${port}`,
      'Upgrade: websocket',
      'Connection: Upgrade',
      `Sec-WebSocket-Key: ${randomBytes(16).toString('base64')}`,
      'Sec-WebSocket-Version: 13',
      '',
      '',
    ].join(CRLF)));
    socket.on('data', (chunk) => {
      if (upgraded) return; // welcome і ws-ping свідомо ігноруємо
      head += chunk.toString('latin1');
      if (!head.includes('101 Switching Protocols')) return;
      upgraded = true;
      socket.write(maskedFrame(JSON.stringify({ m: 'join', session, author, since: 0, proto: 1 })));
      socket.write(maskedFrame(JSON.stringify({
        m: 'client_info', live: '12.3.8', script: '0.18.0', features, events: ['TempoSet'],
      })));
      resolve(socket);
    });
  });
}

async function waitHealth(port, check, ms = 10000) {
  const deadline = Date.now() + ms;
  let last = null;
  while (Date.now() < deadline) {
    last = await fetch(`http://127.0.0.1:${port}/health`).then((r) => r.json());
    if (check(last)) return last;
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error(`/health не дочекався потрібного стану: ${JSON.stringify(last)}`);
}

test('relay віддає features і прибирає мовчазного клієнта', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'abletonmp-relay-heartbeat-'));
  const port = await freePort();
  const relay = spawn(process.execPath, [join(root, 'relay/server.js')], {
    cwd: join(root, 'relay'),
    env: {
      ...process.env,
      MP_RELAY_PORT: String(port),
      MP_JOURNAL_DIR: dir,
      MP_HEARTBEAT_SEC: '1',
      MP_STALE_SEC: '2',
    },
  });
  relay.out = '';
  relay.stdout.on('data', (b) => { relay.out += b.toString(); });
  relay.stderr.on('data', (b) => { relay.out += b.toString(); });

  let deaf = null;
  try {
    await waitForOutput(relay, /relay слухає/);
    deaf = await deafClient(port, {
      session: 'stale', author: 'ghost', features: ['apply_ack', 'ai_chat'],
    });

    const room = (health) => health.sessions.find((s) => s.session === 'stale');
    const joined = room(await waitHealth(port, (h) => room(h)?.clients.length === 1));
    assert.equal(joined.clients[0].author, 'ghost');
    assert.deepEqual(joined.clients[0].features, ['apply_ack', 'ai_chat']);
    assert.equal(joined.peers.length, 1);

    const emptied = room(await waitHealth(port, (h) => room(h)?.online === 0));
    assert.deepEqual(emptied.peers, []);
    assert.deepEqual(emptied.clients, []);
    assert.match(relay.out, /ghost мовчить/);
  } finally {
    if (deaf) deaf.destroy();
    relay.kill();
    rmSync(dir, { recursive: true, force: true });
  }
});

// Клієнт з чергою: локи прилітають broadcast-ом, тож чекати треба конкретне
// повідомлення, а не наступне.
function relayClient(port, session, author) {
  const ws = new WebSocket(`ws://127.0.0.1:${port}`);
  const queue = [];
  let waiters = [];
  const client = {
    ws,
    send: (msg) => ws.send(JSON.stringify(msg)),
    drain: () => { queue.length = 0; },
    take(pred, ms = 5000) {
      return new Promise((resolve, reject) => {
        const scan = () => {
          const i = queue.findIndex(pred);
          if (i < 0) return false;
          resolve(queue.splice(i, 1)[0]);
          return true;
        };
        if (scan()) return;
        const timer = setTimeout(
          () => reject(new Error(`${author} не дочекався повідомлення: ${JSON.stringify(queue)}`)), ms);
        waiters.push(() => {
          if (!scan()) return false;
          clearTimeout(timer);
          return true;
        });
      });
    },
  };
  ws.on('message', (raw) => {
    queue.push(JSON.parse(raw));
    waiters = waiters.filter((w) => !w());
  });
  return new Promise((resolve, reject) => {
    ws.on('error', reject);
    ws.on('open', () => {
      client.send({ m: 'join', session, author, since: 0, proto: 1 });
      client.take((m) => m.m === 'welcome').then(() => resolve(client), reject);
    });
  });
}

test('relay арбітрує локи: чужий не візьме, свій зникає разом із гравцем', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'abletonmp-relay-locks-'));
  const port = await freePort();
  const relay = spawn(process.execPath, [join(root, 'relay/server.js')], {
    cwd: join(root, 'relay'),
    env: { ...process.env, MP_RELAY_PORT: String(port), MP_JOURNAL_DIR: dir, MP_HEARTBEAT_SEC: '1' },
  });
  relay.out = '';
  relay.stdout.on('data', (b) => { relay.out += b.toString(); });
  relay.stderr.on('data', (b) => { relay.out += b.toString(); });

  let p1 = null;
  let p2 = null;
  try {
    await waitForOutput(relay, /relay слухає/);
    p1 = await relayClient(port, 'locks', 'p1');
    p2 = await relayClient(port, 'locks', 'p2');

    p1.send({ m: 'lock', object: 'track:a', label: 'Bass' });
    const taken = await p2.take((m) => m.m === 'locks');
    assert.equal(taken.locks.length, 1);
    assert.equal(taken.locks[0].author, 'p1');
    assert.equal(taken.locks[0].label, 'Bass');

    const health = await fetch(`http://127.0.0.1:${port}/health`).then((r) => r.json());
    assert.equal(health.sessions.find((s) => s.session === 'locks').locks[0].object, 'track:a');

    p2.send({ m: 'lock', object: 'track:a' });
    const denied = await p2.take((m) => m.m === 'lock_denied');
    assert.equal(denied.author, 'p1');
    assert.equal(denied.object, 'track:a');

    // Власний лок не впирається сам у себе: жест триває, лок поновлюється.
    p1.drain();
    p1.send({ m: 'lock', object: 'track:a' });
    const renewed = await p1.take((m) => m.m === 'locks' && m.locks[0]?.object === 'track:a');
    assert.equal(renewed.locks[0].author, 'p1');

    // Лок із коротким TTL знімається сам -- гравець "завис" з ним назавжди.
    p2.drain();
    p2.send({ m: 'lock', object: 'track:b', ttl: 1 });
    await p2.take((m) => m.m === 'locks' && m.locks.some((l) => l.object === 'track:b'));
    p2.drain();
    const expired = await p2.take(
      (m) => m.m === 'locks' && !m.locks.some((l) => l.object === 'track:b'), 8000);
    assert.deepEqual(expired.locks.map((l) => l.object), ['track:a']);

    // Гравець пішов -- його лок пішов з ним, не чекаючи TTL.
    p2.drain();
    p1.ws.close();
    const afterLeave = await p2.take((m) => m.m === 'locks' && m.locks.length === 0, 5000);
    assert.deepEqual(afterLeave.locks, []);

    // Чужий лок зняти не можна.
    p2.drain();
    p2.send({ m: 'lock', object: 'track:c' });
    await p2.take((m) => m.m === 'locks' && m.locks.some((l) => l.object === 'track:c'));
    const alone = await relayClient(port, 'locks', 'p3');
    alone.send({ m: 'unlock', object: 'track:c' });
    alone.send({ m: 'lock', object: 'track:d' });
    const still = await alone.take((m) => m.m === 'locks' && m.locks.some((l) => l.object === 'track:d'));
    assert.equal(still.locks.some((l) => l.object === 'track:c' && l.author === 'p2'), true);
    alone.ws.close();
  } finally {
    if (p1) p1.ws.close();
    if (p2) p2.ws.close();
    relay.kill();
    rmSync(dir, { recursive: true, force: true });
  }
});
