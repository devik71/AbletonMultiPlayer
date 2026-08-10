import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import {
  existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, utimesSync, writeFileSync,
} from 'node:fs';
import { createServer } from 'node:net';
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
