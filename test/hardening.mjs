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
function relayClient(port, session, author, token) {
  const ws = new WebSocket(`ws://127.0.0.1:${port}`);
  const queue = [];
  let waiters = [];
  const client = {
    ws,
    send: (msg) => ws.send(JSON.stringify(msg)),
    drain: () => { queue.length = 0; },
    all: () => [...queue],
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
      client.send({ m: 'join', session, author, since: 0, proto: 1, token });
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

function spawnRelay(dir, port, env = {}) {
  const relay = spawn(process.execPath, [join(root, 'relay/server.js')], {
    cwd: join(root, 'relay'),
    env: { ...process.env, MP_RELAY_PORT: String(port), MP_JOURNAL_DIR: dir, ...env },
  });
  relay.out = '';
  relay.stdout.on('data', (b) => { relay.out += b.toString(); });
  relay.stderr.on('data', (b) => { relay.out += b.toString(); });
  return relay;
}

test('relay з токеном пускає лише своїх', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'abletonmp-relay-token-'));
  const port = await freePort();
  const relay = spawnRelay(dir, port, { MP_RELAY_TOKEN: 'sekret' });
  let allowed = null;
  try {
    await waitForOutput(relay, /relay слухає/);

    const bare = await exchange(port, { m: 'join', session: 'guarded', author: 'p1', since: 0, proto: 1 });
    assert.equal(bare.error.code, 'bad_token');
    const wrong = await exchange(port,
      { m: 'join', session: 'guarded', author: 'p1', since: 0, proto: 1, token: 'inshyi' });
    assert.equal(wrong.error.code, 'bad_token');

    allowed = await relayClient(port, 'guarded', 'p1', 'sekret');
    assert.equal(allowed.all().length, 0, 'після welcome більше нічого не прилетіло');
    assert.match(relay.out, /join відхилено: невірний токен/);
    assert.equal(/sekret/.test(relay.out), false, 'токен не має світитись у лозі');
  } finally {
    if (allowed) allowed.ws.close();
    relay.kill();
    rmSync(dir, { recursive: true, force: true });
  }
});

test('relay гальмує флуд, але подія не губиться', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'abletonmp-relay-flood-'));
  const port = await freePort();
  const relay = spawnRelay(dir, port, { MP_SUBMIT_RATE: '5', MP_SUBMIT_BURST: '5' });
  let p1 = null;
  try {
    await waitForOutput(relay, /relay слухає/);
    p1 = await relayClient(port, 'flood', 'p1');

    for (let lseq = 1; lseq <= 20; lseq += 1) {
      p1.send({ m: 'submit', event: { type: 'TempoSet', payload: { bpm: 100 + lseq }, lseq } });
    }
    const limited = await p1.take((m) => m.m === 'error' && m.code === 'rate_limited');
    assert.match(limited.text, /5 подій\/с/);

    await new Promise((resolve) => setTimeout(resolve, 400));
    const lines = readFileSync(join(dir, 'flood.jsonl'), 'utf8').split('\n').filter(Boolean);
    assert.ok(lines.length >= 5 && lines.length <= 7, `у журналі ${lines.length} подій замість ~5`);

    // Відхилена подія не вважається побаченою: після паузи вона проходить
    // тим самим lseq, а не залипає як дублікат.
    p1.drain();
    await new Promise((resolve) => setTimeout(resolve, 1100));
    p1.send({ m: 'submit', event: { type: 'TempoSet', payload: { bpm: 200 }, lseq: 20 } });
    const commit = await p1.take((m) => m.m === 'commit' && m.event.lseq === 20);
    assert.equal(commit.event.payload.bpm, 200);
  } finally {
    if (p1) p1.ws.close();
    relay.kill();
    rmSync(dir, { recursive: true, force: true });
  }
});

test('файловий чанк іде адресату, а не всій кімнаті', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'abletonmp-relay-files-'));
  const port = await freePort();
  const relay = spawnRelay(dir, port);
  const room = [];
  try {
    await waitForOutput(relay, /relay слухає/);
    for (const author of ['a', 'b', 'c']) room.push(await relayClient(port, 'files', author, undefined));
    const [a, b, c] = room;
    b.drain();
    c.drain();

    a.send({ m: 'file_chunk', to: 'b', path: 'Samples/kick.wav', seq: 0, total: 1, data: 'AA==' });
    // Маніфест адресата не має -- він лишається broadcast-ом і доїде до всіх.
    a.send({ m: 'files_manifest', files: [{ path: 'Samples/kick.wav', size: 1, hash: 'ab' }] });

    const chunk = await b.take((m) => m.m === 'file_chunk');
    assert.equal(chunk.from, 'a');
    const manifest = await c.take((m) => m.m === 'files_manifest');
    assert.equal(manifest.from, 'a');
    assert.equal(c.all().some((m) => m.m === 'file_chunk'), false, 'чужий чанк долетів до третього');
  } finally {
    for (const client of room) client.ws.close();
    relay.kill();
    rmSync(dir, { recursive: true, force: true });
  }
});

test('порожня сесія вивантажується з памʼяті, журнал і head лишаються', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'abletonmp-relay-evict-'));
  const port = await freePort();
  const relay = spawnRelay(dir, port, { MP_HEARTBEAT_SEC: '1', MP_SESSION_IDLE_SEC: '1' });
  try {
    await waitForOutput(relay, /relay слухає/);

    const first = await relayClient(port, 'ephemeral', 'p1');
    first.send({ m: 'submit', event: { type: 'TempoSet', payload: { bpm: 124 }, lseq: 1 } });
    const commit = await first.take((m) => m.m === 'commit');
    assert.equal(commit.event.gseq, 1);
    first.ws.close();

    await waitForOutput(relay, /ephemeral. порожня \d+ с/, 8000);
    const health = await fetch(`http://127.0.0.1:${port}/health`).then((r) => r.json());
    assert.equal(health.sessions.some((s) => s.session === 'ephemeral'), false, 'сесія лишилась у памʼяті');
    const onDisk = health.journals.find((j) => j.session === 'ephemeral');
    assert.ok(onDisk && onDisk.bytes > 0, 'журнал зник із диска');
    assert.equal(onDisk.in_memory, false);

    // Наступний join піднімає сесію з диска: gseq триває, дедуплікація ціла.
    const second = await relayClient(port, 'ephemeral', 'p2');
    second.send({ m: 'submit', event: { type: 'TempoSet', payload: { bpm: 126 }, lseq: 1 } });
    const next = await second.take((m) => m.m === 'commit' && m.event.author === 'p2');
    assert.equal(next.event.gseq, 2);
    assert.equal(next.event.prev_hash, commit.event.hash);
    second.ws.close();
  } finally {
    relay.kill();
    rmSync(dir, { recursive: true, force: true });
  }
});

const jsonl = (file) => readFileSync(file, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l));

test('журнал стискається, історія лишається в архіві й переживає рестарт', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'abletonmp-relay-compact-'));
  const port = await freePort();
  let relay = spawnRelay(dir, port, { MP_COMPACT_AT: '10' });
  const open = [];
  try {
    await waitForOutput(relay, /relay слухає/);
    const p1 = await relayClient(port, 'big', 'p1');
    open.push(p1);

    // 30 рухів одного фейдера -- усе, крім останнього, перекрите
    for (let lseq = 1; lseq <= 30; lseq += 1) {
      p1.send({
        m: 'submit',
        event: { type: 'MixerSet', payload: { track: { id: 't1' }, param: 'volume', value: lseq / 100 }, lseq },
      });
    }
    const head = await p1.take((m) => m.m === 'commit' && m.event.gseq === 30, 8000);
    await waitForOutput(relay, /журнал стиснуто/, 8000);

    const live = jsonl(join(dir, 'big.jsonl'));
    assert.ok(live.length < 10, `у живому журналі лишилось ${live.length} подій`);
    assert.equal(live.at(-1).gseq, 30, 'head має лишатись у живому журналі');
    assert.equal(live.at(-1).hash, head.event.hash);

    const archived = new Set(jsonl(join(dir, 'big.archive.jsonl')).map((ev) => ev.gseq));
    for (let gseq = 1; gseq <= 30; gseq += 1) {
      assert.ok(archived.has(gseq), `холодний архів втратив подію #${gseq}`);
    }

    // Новий учасник усе одно доходить до фінального значення
    const fresh = await relayClient(port, 'big', 'p2');
    open.push(fresh);
    const arrived = await fresh.take((m) => m.m === 'commit' && m.event.gseq === 30);
    assert.equal(arrived.event.payload.value, 0.3);

    for (const client of open.splice(0)) client.ws.close();
    relay.kill();
    await new Promise((resolve) => relay.once('exit', resolve));

    relay = spawnRelay(dir, port, { MP_COMPACT_AT: '10' });
    await waitForOutput(relay, /relay слухає/);
    const p3 = await relayClient(port, 'big', 'p3');
    open.push(p3);
    assert.match(relay.out, /стиснутий \(архів до #30\)/);

    p3.send({ m: 'submit', event: { type: 'TempoSet', payload: { bpm: 131 }, lseq: 1 } });
    const next = await p3.take((m) => m.m === 'commit' && m.event.author === 'p3');
    assert.equal(next.event.gseq, 31, 'нумерація має тривати');
    assert.equal(next.event.prev_hash, head.event.hash, 'ланцюг має чіплятись за head');

    // Дедуплікація пережила і стиснення, і рестарт: подія, якої вже немає
    // в живому журналі, не комітиться вдруге.
    const revenant = await relayClient(port, 'big', 'p1');
    open.push(revenant);
    revenant.drain();
    revenant.send({
      m: 'submit',
      event: { type: 'MixerSet', payload: { track: { id: 't1' }, param: 'volume', value: 0.01 }, lseq: 3 },
    });
    const ack = await revenant.take((m) => m.m === 'ack');
    assert.equal(ack.lseq, 3);
    const afterAck = jsonl(join(dir, 'big.jsonl'));
    assert.equal(afterAck.at(-1).gseq, 31, 'стара подія не мала лягти в журнал');
  } finally {
    for (const client of open) client.ws.close();
    relay.kill();
    rmSync(dir, { recursive: true, force: true });
  }
});

test('зниклий рядок у стиснутому журналі помічається', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'abletonmp-relay-tamper-'));
  const port = await freePort();
  let relay = spawnRelay(dir, port, { MP_COMPACT_AT: '10' });
  const open = [];
  try {
    await waitForOutput(relay, /relay слухає/);
    const p1 = await relayClient(port, 'big', 'p1');
    open.push(p1);
    for (let lseq = 1; lseq <= 30; lseq += 1) {
      p1.send({
        m: 'submit',
        event: { type: 'MixerSet', payload: { track: { id: `t${lseq % 3}` }, param: 'volume', value: lseq / 100 }, lseq },
      });
    }
    await p1.take((m) => m.m === 'commit' && m.event.gseq === 30, 8000);
    await waitForOutput(relay, /журнал стиснуто/, 8000);

    for (const client of open.splice(0)) client.ws.close();
    relay.kill();
    await new Promise((resolve) => relay.once('exit', resolve));

    // Прибираємо один рядок: дірки в стиснутому журналі законні, тож ловить
    // це не gseq, а кількість подій у checkpoint.
    const kept = readFileSync(join(dir, 'big.jsonl'), 'utf8').split('\n').filter(Boolean);
    writeFileSync(join(dir, 'big.jsonl'), kept.slice(1).join('\n') + '\n');

    relay = spawnRelay(dir, port, { MP_COMPACT_AT: '10' });
    await waitForOutput(relay, /relay слухає/);
    const p2 = await relayClient(port, 'big', 'p2');
    open.push(p2);
    p2.send({ m: 'submit', event: { type: 'TempoSet', payload: { bpm: 140 }, lseq: 1 } });
    const failed = await p2.take((m) => m.m === 'error');
    assert.equal(failed.code, 'journal_write_failed');

    const health = await fetch(`http://127.0.0.1:${port}/health`).then((r) => r.json());
    assert.match(health.sessions.find((s) => s.session === 'big').journal_error, /подій замість/);
  } finally {
    for (const client of open) client.ws.close();
    relay.kill();
    rmSync(dir, { recursive: true, force: true });
  }
});

test('verify.js підтверджує цілісність і ловить підміну в архіві', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'abletonmp-relay-verify-'));
  const port = await freePort();
  const relay = spawnRelay(dir, port, { MP_COMPACT_AT: '10' });
  const open = [];

  const verify = (args = []) => new Promise((resolve) => {
    const proc = spawn(process.execPath, [join(root, 'relay/verify.js'), 'audit', '--dir', dir, ...args],
      { cwd: join(root, 'relay') });
    let out = '';
    proc.stdout.on('data', (b) => { out += b.toString(); });
    proc.stderr.on('data', (b) => { out += b.toString(); });
    proc.on('close', (code) => resolve({ code, out }));
  });

  try {
    await waitForOutput(relay, /relay слухає/);
    const p1 = await relayClient(port, 'audit', 'p1');
    open.push(p1);
    for (let lseq = 1; lseq <= 30; lseq += 1) {
      p1.send({
        m: 'submit',
        event: { type: 'MixerSet', payload: { track: { id: 't1' }, param: 'volume', value: lseq / 100 }, lseq },
      });
    }
    await p1.take((m) => m.m === 'commit' && m.event.gseq === 30, 8000);
    await waitForOutput(relay, /журнал стиснуто/, 8000);
    for (const client of open.splice(0)) client.ws.close();
    relay.kill();
    await new Promise((resolve) => relay.once('exit', resolve));

    const ok = await verify();
    assert.equal(ok.code, 0, ok.out);
    assert.match(ok.out, /цілісність підтверджена/);
    assert.match(ok.out, /стиснутий/);

    // Підміна значення в холодному архіві: подія перестає збігатися з хешем
    const archivePath = join(dir, 'audit.archive.jsonl');
    const cold = readFileSync(archivePath, 'utf8').split('\n').filter(Boolean);
    const forged = JSON.parse(cold[4]);
    forged.payload.value = 0.999;
    cold[4] = JSON.stringify(forged);
    writeFileSync(archivePath, cold.join('\n') + '\n');

    const broken = await verify();
    assert.equal(broken.code, 1);
    assert.match(broken.out, /не збігається зі своїм хешем/);
  } finally {
    for (const client of open) client.ws.close();
    relay.kill();
    rmSync(dir, { recursive: true, force: true });
  }
});

test('/health під токеном не віддає нічого без токена', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'abletonmp-relay-health-'));
  const port = await freePort();
  const relay = spawnRelay(dir, port, { MP_RELAY_TOKEN: 'sekret' });
  try {
    await waitForOutput(relay, /relay слухає/);
    const bare = await fetch(`http://127.0.0.1:${port}/health`);
    assert.equal(bare.status, 401);

    const wrong = await fetch(`http://127.0.0.1:${port}/health?token=inshyi`);
    assert.equal(wrong.status, 401);

    const byQuery = await fetch(`http://127.0.0.1:${port}/health?token=sekret`);
    assert.equal(byQuery.status, 200);
    assert.equal((await byQuery.json()).ok, true);

    const byHeader = await fetch(`http://127.0.0.1:${port}/health`, { headers: { 'x-relay-token': 'sekret' } });
    assert.equal(byHeader.status, 200);
  } finally {
    relay.kill();
    rmSync(dir, { recursive: true, force: true });
  }
});

test('MP_FSYNC=1 не змінює вміст журналу, лише шлях запису', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'abletonmp-relay-fsync-'));
  const port = await freePort();
  const relay = spawnRelay(dir, port, { MP_FSYNC: '1' });
  let p1 = null;
  try {
    await waitForOutput(relay, /relay слухає/);
    p1 = await relayClient(port, 'durable', 'p1');
    p1.send({ m: 'submit', event: { type: 'TempoSet', payload: { bpm: 128 }, lseq: 1 } });
    p1.send({ m: 'submit', event: { type: 'TempoSet', payload: { bpm: 129 }, lseq: 2 } });
    const second = await p1.take((m) => m.m === 'commit' && m.event.gseq === 2);

    const lines = readFileSync(join(dir, 'durable.jsonl'), 'utf8').split('\n').filter(Boolean);
    assert.equal(lines.length, 2);
    const written = JSON.parse(lines[1]);
    assert.deepEqual(written, second.event, 'подія на диску має збігатись із розісланою');
    assert.equal(written.prev_hash, JSON.parse(lines[0]).hash);
  } finally {
    if (p1) p1.ws.close();
    relay.kill();
    rmSync(dir, { recursive: true, force: true });
  }
});
