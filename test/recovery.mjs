import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { createSocket } from 'node:dgram';
import { mkdirSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import WebSocket from '../daemon/node_modules/ws/index.js';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');

const canonical = (value) => {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  return '{' + Object.keys(value).sort()
    .map((key) => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
};

function launch(name, file, args = [], opts = {}) {
  const proc = spawn(process.execPath, [file, ...args], {
    cwd: opts.cwd || root,
    env: { ...process.env, ...opts.env },
  });
  proc.name = name;
  proc.out = '';
  const collect = (buf) => { proc.out += buf.toString(); };
  proc.stdout.on('data', collect);
  proc.stderr.on('data', collect);
  return proc;
}

function waitFor(proc, pattern, ms = 10000, from = 0) {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + ms;
    const timer = setInterval(() => {
      if (pattern.test(proc.out.slice(from))) {
        clearInterval(timer);
        resolve();
      } else if (proc.exitCode !== null) {
        clearInterval(timer);
        reject(new Error(`${proc.name} завершився до ${pattern}\n${proc.out}`));
      } else if (Date.now() > deadline) {
        clearInterval(timer);
        reject(new Error(`${proc.name} не дочекався ${pattern}\n${proc.out}`));
      }
    }, 30);
  });
}

async function stop(proc) {
  if (!proc || proc.exitCode !== null) return;
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error(`${proc.name} не завершився`)), 5000);
    proc.once('exit', () => {
      clearTimeout(timeout);
      resolve();
    });
    proc.kill();
  });
}

function freeTcpPort() {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close((error) => error ? reject(error) : resolve(port));
    });
  });
}

function freeUdpPort() {
  return new Promise((resolve, reject) => {
    const socket = createSocket('udp4');
    socket.on('error', reject);
    socket.bind(0, '127.0.0.1', () => {
      const { port } = socket.address();
      socket.close(() => resolve(port));
    });
  });
}

function submitEvents(port, session, events) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`ws://127.0.0.1:${port}`);
    let index = 0;
    const timeout = setTimeout(() => {
      ws.close();
      reject(new Error(`не вдалося засіяти ${events.length} подій`));
    }, 5000);
    const submit = () => ws.send(JSON.stringify({
      m: 'submit', event: { ...events[index], lseq: index + 1 },
    }));
    ws.on('open', () => ws.send(JSON.stringify({
      m: 'join', session, author: 'seed', since: 0, proto: 1,
    })));
    ws.on('message', (raw) => {
      const msg = JSON.parse(raw);
      if (msg.m === 'welcome') submit();
      else if (msg.m === 'commit' && msg.event.author === 'seed') {
        index += 1;
        if (index < events.length) submit();
        else {
          clearTimeout(timeout);
          ws.close();
          resolve();
        }
      } else if (msg.m === 'error') {
        clearTimeout(timeout);
        ws.close();
        reject(new Error(`relay: ${msg.code} ${msg.text}`));
      }
    });
    ws.on('error', reject);
  });
}

test('relay і daemon продовжують журнал та outbox після повного рестарту', async () => {
  const tmp = mkdtempSync(join(tmpdir(), 'abletonmp-recovery-'));
  const relayPort = await freeTcpPort();
  const daemonPort = await freeUdpPort();
  let livePort = await freeUdpPort();
  while (livePort === daemonPort) livePort = await freeUdpPort();
  const session = 'recovery';
  const project = join(tmp, 'project');
  mkdirSync(join(project, 'Samples'), { recursive: true });

  const processes = new Set();
  const startRelay = (name) => {
    const proc = launch(name, join(root, 'relay/server.js'), [], {
      cwd: join(root, 'relay'),
      env: { MP_RELAY_PORT: String(relayPort), MP_JOURNAL_DIR: tmp },
    });
    processes.add(proc);
    return proc;
  };
  const startDaemon = (name) => {
    const proc = launch(name, join(root, 'daemon/index.js'), [
      '--author', 'p1', '--session', session,
      '--relay', `ws://127.0.0.1:${relayPort}`,
      '--udp-in', String(daemonPort), '--udp-out', String(livePort),
      '--state-dir', tmp, '--project', project,
    ], { cwd: join(root, 'daemon') });
    processes.add(proc);
    return proc;
  };

  let relay = startRelay('relay-1');
  let daemon;
  let live;
  try {
    await waitFor(relay, /relay слухає/);
    daemon = startDaemon('daemon-1');
    await waitFor(daemon, /relay: head=0/);
    live = launch('fake-live', join(root, 'daemon/tools/fake-live.js'), [
      '--udp-in', String(daemonPort), '--udp-out', String(livePort),
    ], { cwd: join(root, 'daemon') });
    processes.add(live);
    await waitFor(daemon, /#1 RegistryInit/);

    const daemonCloseFrom = daemon.out.length;
    await stop(relay);
    processes.delete(relay);
    await waitFor(daemon, /звʼязок з relay втрачено/, 5000, daemonCloseFrom);

    const offlineFrom = daemon.out.length;
    live.stdin.write('tempo 137\n');
    await waitFor(daemon, /офлайн, у буфер: TempoSet/, 5000, offlineFrom);
    const outboxPath = join(tmp, `${session === 'default' ? 'p1' : `p1.${session}`}.outbox.jsonl`);
    assert.match(readFileSync(outboxPath, 'utf8'), /"type":"TempoSet"/);

    await stop(daemon);
    processes.delete(daemon);

    relay = startRelay('relay-2');
    await waitFor(relay, /relay слухає/);
    daemon = startDaemon('daemon-2');
    await waitFor(relay, /журнал відновлено: 1 подій, head=1/);
    await waitFor(daemon, /відновлено 1 невідправлених подій з outbox/);
    await waitFor(relay, /#2 TempoSet .*"bpm":137/);
    await waitFor(daemon, /#2 TempoSet \(моя, ack\)/);
    assert.equal(readFileSync(outboxPath, 'utf8'), '');

    const journal = readFileSync(join(tmp, `${session}.jsonl`), 'utf8')
      .split('\n').filter(Boolean).map(JSON.parse);
    assert.equal(journal.length, 2);
    let previous = '';
    journal.forEach((event, index) => {
      const { hash, prev_hash: prevHash, ...body } = event;
      assert.equal(body.gseq, index + 1);
      assert.equal(prevHash, previous);
      assert.equal(hash, createHash('sha256').update(prevHash + canonical(body)).digest('hex'));
      previous = hash;
    });
    const checkpoint = JSON.parse(readFileSync(join(tmp, `${session}.checkpoint.json`), 'utf8'));
    assert.equal(checkpoint.gseq, journal.at(-1).gseq);
    assert.equal(checkpoint.hash, journal.at(-1).hash);
  } finally {
    await Promise.allSettled([...processes].map(stop));
    rmSync(tmp, { recursive: true, force: true });
  }
});

test('незастосована подія переживає рестарт daemon до появи bridge', async () => {
  const tmp = mkdtempSync(join(tmpdir(), 'abletonmp-pending-recovery-'));
  const relayPort = await freeTcpPort();
  const daemonPort = await freeUdpPort();
  let livePort = await freeUdpPort();
  while (livePort === daemonPort) livePort = await freeUdpPort();
  const session = 'pending-recovery';
  const author = 'receiver';
  const project = join(tmp, 'project');
  mkdirSync(join(project, 'Samples'), { recursive: true });

  const registry = {
    tracks: ['1-MIDI', '2-MIDI', '3-Audio'].map((name, idx) => ({
      id: `10000000000${idx}`, idx, name,
    })),
    scenes: Array.from({ length: 8 }, (_, idx) => ({
      id: `20000000000${idx}`, idx, name: `Scene ${idx + 1}`,
    })),
  };

  const processes = new Set();
  const relay = launch('pending-relay', join(root, 'relay/server.js'), [], {
    cwd: join(root, 'relay'),
    env: { MP_RELAY_PORT: String(relayPort), MP_JOURNAL_DIR: tmp },
  });
  processes.add(relay);
  const startDaemon = (name) => {
    const proc = launch(name, join(root, 'daemon/index.js'), [
      '--author', author, '--session', session,
      '--relay', `ws://127.0.0.1:${relayPort}`,
      '--udp-in', String(daemonPort), '--udp-out', String(livePort),
      '--state-dir', tmp, '--project', project,
    ], { cwd: join(root, 'daemon') });
    processes.add(proc);
    return proc;
  };

  let daemon;
  try {
    await waitFor(relay, /relay слухає/);
    await submitEvents(relayPort, session, [
      { type: 'RegistryInit', payload: registry },
      { type: 'TempoSet', payload: { bpm: 141 } },
    ]);

    daemon = startDaemon('pending-daemon-1');
    await waitFor(daemon, /#2 TempoSet/);
    const pendingPath = join(tmp, `${author}.${session}.pending.jsonl`);
    assert.match(readFileSync(pendingPath, 'utf8'), /"gseq":2/);

    await stop(daemon);
    processes.delete(daemon);
    daemon = startDaemon('pending-daemon-2');
    await waitFor(daemon, /відновлено 1 незастосованих подій для bridge/);
    await waitFor(daemon, /relay: head=2/);

    const live = launch('pending-live', join(root, 'daemon/tools/fake-live.js'), [
      '--udp-in', String(daemonPort), '--udp-out', String(livePort),
    ], { cwd: join(root, 'daemon') });
    processes.add(live);
    await waitFor(live, /<- #2 TempoSet \{"bpm":141\}/);
    await waitFor(daemon, /застосовую 1 відкладених подій/);
    const clearedAt = Date.now() + 5000;
    while (readFileSync(pendingPath, 'utf8') !== '' && Date.now() < clearedAt) {
      await new Promise((resolve) => setTimeout(resolve, 30));
    }
    assert.equal(readFileSync(pendingPath, 'utf8'), '');
  } finally {
    await Promise.allSettled([...processes].map(stop));
    rmSync(tmp, { recursive: true, force: true });
  }
});
