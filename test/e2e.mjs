// E2E: relay + 2 daemon + 2 fake-live, весь ланцюг без Live.
//
//   node test/e2e.mjs
//
// Перевіряє, що подія одного гравця доїжджає до другого, що порядок задає relay,
// і що журнал зшитий у коректний hash-chain.

import { spawn } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const tmp = mkdtempSync(join(tmpdir(), 'abletonmp-e2e-'));
const RELAY_PORT = 19970;
const SESSION = 'e2e';

const procs = [];
let failed = 0;

function launch(name, file, args, opts = {}) {
  const p = spawn(process.execPath, [file, ...args], { cwd: opts.cwd || root, env: { ...process.env, ...opts.env } });
  p.out = '';
  const collect = (buf) => {
    p.out += buf.toString();
    if (process.env.E2E_VERBOSE) process.stdout.write(`[${name}] ${buf}`);
  };
  p.stdout.on('data', collect);
  p.stderr.on('data', collect);
  p.name = name;
  procs.push(p);
  return p;
}

function waitFor(p, pattern, ms = 8000) {
  const start = p.out.length ? 0 : 0;
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + ms;
    const tick = setInterval(() => {
      if (pattern.test(p.out.slice(start))) {
        clearInterval(tick);
        resolve();
      } else if (Date.now() > deadline) {
        clearInterval(tick);
        reject(new Error(`[${p.name}] не дочекався ${pattern}\n--- вивід ---\n${p.out}`));
      }
    }, 50);
  });
}

async function check(label, fn) {
  try {
    await fn();
    console.log(`  ok   ${label}`);
  } catch (e) {
    failed += 1;
    console.log(`  FAIL ${label}\n       ${e.message.split('\n')[0]}`);
    if (process.env.E2E_VERBOSE) console.log(e.message);
  }
}

function cleanup() {
  for (const p of procs) {
    try {
      p.kill();
    } catch {}
  }
  try {
    rmSync(tmp, { recursive: true, force: true });
  } catch {}
}

const canonical = (v) => {
  if (v === null || typeof v !== 'object') return JSON.stringify(v);
  if (Array.isArray(v)) return '[' + v.map(canonical).join(',') + ']';
  return '{' + Object.keys(v).sort().map((k) => JSON.stringify(k) + ':' + canonical(v[k])).join(',') + '}';
};

// ------------------------------------------------------------------- сценарій

try {
  console.log(`tmp: ${tmp}\n`);

  const relay = launch('relay', join(root, 'relay/server.js'), [], {
    cwd: join(root, 'relay'),
    env: { MP_RELAY_PORT: String(RELAY_PORT), MP_JOURNAL_DIR: tmp },
  });
  await waitFor(relay, /relay слухає/);

  const mkDaemon = (author, inPort, outPort) =>
    launch(`daemon-${author}`, join(root, 'daemon/index.js'), [
      '--author', author, '--session', SESSION,
      '--relay', `ws://127.0.0.1:${RELAY_PORT}`,
      '--udp-in', String(inPort), '--udp-out', String(outPort),
      '--state-dir', tmp,
    ], { cwd: join(root, 'daemon') });

  const d1 = mkDaemon('p1', 19945, 19946);
  const d2 = mkDaemon('p2', 19947, 19948);
  await Promise.all([waitFor(d1, /relay: head=/), waitFor(d2, /relay: head=/)]);

  const mkLive = (n, inPort, outPort) =>
    launch(`live-${n}`, join(root, 'daemon/tools/fake-live.js'),
      ['--udp-in', String(inPort), '--udp-out', String(outPort)], { cwd: join(root, 'daemon') });

  const l1 = mkLive(1, 19945, 19946);
  const l2 = mkLive(2, 19947, 19948);
  await Promise.all([waitFor(d1, /bridge підключився/), waitFor(d2, /bridge підключився/)]);

  console.log('ланцюг піднявся\n');

  await check('tempo від p1 доїхав до p2', async () => {
    l1.stdin.write('tempo 128\n');
    await waitFor(l2, /<- #\d+ TempoSet \{"bpm":128\}/);
  });

  await check('clip launch від p1 доїхав до p2', async () => {
    l1.stdin.write('launch 1 2\n');
    await waitFor(l2, /<- #\d+ ClipLaunch .*"idx":1.*"name":"2-MIDI"/);
  });

  await check('transport від p2 доїхав до p1', async () => {
    l2.stdin.write('play\n');
    await waitFor(l1, /<- #\d+ TransportSet \{"playing":true\}/);
  });

  await check('автор не отримує власну подію назад в LOM', async () => {
    await waitFor(d1, /#\d+ TempoSet \(моя, ack\)/);
    if (/<- #\d+ TempoSet/.test(l1.out)) throw new Error('p1 застосував власний TempoSet — ехо');
  });

  await check('scene launch доїхав як одна подія, а не пачка ClipLaunch', async () => {
    l1.stdin.write('scene 3\n');
    await waitFor(l2, /<- #\d+ SceneLaunch .*"idx":3/);
    if (/<- #\d+ ClipLaunch .*"scene":\{"idx":3/.test(l2.out)) {
      throw new Error('сцена розсипалась на окремі ClipLaunch');
    }
  });

  await check('stop all доїхав як одна подія', async () => {
    l1.stdin.write('stopall\n');
    await waitFor(l2, /<- #\d+ StopAllClips/);
    if (l2.out.match(/<- #\d+ ClipStop/g)) throw new Error('стоп розсипався на окремі ClipStop');
  });

  await check('журнал: 5 подій, монотонний gseq, цілий hash-chain', async () => {
    await new Promise((r) => setTimeout(r, 400));
    const lines = readFileSync(join(tmp, `${SESSION}.jsonl`), 'utf8').split('\n').filter(Boolean);
    if (lines.length !== 5) throw new Error(`очікував 5 подій, у журналі ${lines.length}`);
    let prev = '';
    lines.forEach((line, i) => {
      const { hash, prev_hash: ph, ...body } = JSON.parse(line);
      if (body.gseq !== i + 1) throw new Error(`gseq не монотонний: ${body.gseq} на позиції ${i + 1}`);
      if (ph !== prev) throw new Error(`prev_hash розірваний на gseq=${body.gseq}`);
      const want = createHash('sha256').update(ph + canonical(body)).digest('hex');
      if (want !== hash) throw new Error(`hash не збігається на gseq=${body.gseq}`);
      prev = hash;
    });
  });

  await check('розсинхрон адреси відхиляється, а не застосовується не туди', async () => {
    l1.stdin.write('launch 9 0\n');
    await waitFor(l1, /немає такого треку/, 2000);
  });
} catch (e) {
  failed += 1;
  console.log(`\nсценарій обірвався: ${e.message}`);
} finally {
  cleanup();
}

console.log(failed ? `\n${failed} перевірок впало` : '\nусі перевірки пройшли');
process.exit(failed ? 1 : 0);
