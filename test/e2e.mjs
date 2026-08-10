// E2E: relay + 2 daemon + 2 fake-live, весь ланцюг без Live.
//
//   node test/e2e.mjs
//
// Перевіряє, що подія одного гравця доїжджає до другого, що порядок задає relay,
// і що журнал зшитий у коректний hash-chain.

import { spawn } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import WebSocket from '../daemon/node_modules/ws/index.js';

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

/** `from` -- зсув у буфері, з якого шукати. Без нього перевірка може збігтися
 *  зі старим рядком і пройти фіктивно. */
function waitFor(p, pattern, ms = 8000, from = 0) {
  const start = from;
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

  // окрема "тека проєкту" на кожного гравця -- саме між ними ходитимуть файли
  const projectOf = (author) => join(tmp, `project-${author}`);
  for (const a of ['p1', 'p2', 'p3']) mkdirSync(join(projectOf(a), 'Samples'), { recursive: true });

  const mkDaemon = (author, inPort, outPort) =>
    launch(`daemon-${author}`, join(root, 'daemon/index.js'), [
      '--author', author, '--session', SESSION,
      '--relay', `ws://127.0.0.1:${RELAY_PORT}`,
      '--udp-in', String(inPort), '--udp-out', String(outPort),
      '--state-dir', tmp, '--project', projectOf(author),
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

  // Live, запущений ДО daemon: hello летить у порожнечу, лишається тільки heartbeat.
  // Саме так виглядає перезапуск daemon при відкритому DAW.
  const lEarly = mkLive(3, 19949, 19950);
  await waitFor(lEarly, /слухаю :19950/);
  const dLate = mkDaemon('p3', 19949, 19950);
  await waitFor(dLate, /relay: head=/);

  console.log('ланцюг піднявся\n');

  await check('реєстр: один створює, другий приймає', async () => {
    await Promise.all([
      waitFor(l1, /реєстр прийнято$/m),
      waitFor(l2, /реєстр прийнято$/m),
    ]);
    if (!/реєстр створено/.test(l1.out) && !/реєстр створено/.test(l2.out)) {
      throw new Error('жоден bridge не створив початковий реєстр');
    }
    if (/незіставлено/.test(l1.out) || /незіставлено/.test(l2.out)) {
      throw new Error('реєстр прийнято з розбіжностями');
    }
  });

  await check('daemon, стартований при живому Live, теж отримує реєстр', async () => {
    // без бутстрапу з heartbeat-гілки ця сесія лишилась би без ідентичності
    await waitFor(dLate, /віддаю bridge реєстр сесії/, 12000);
    await waitFor(lEarly, /реєстр прийнято/, 12000);
  });

  await check('повторний RegistryInit відхиляється relay', async () => {
    if (!/RegistryInit/.test(relay.out)) throw new Error('RegistryInit не потрапив у журнал');
    const commits = (relay.out.match(/#\d+ RegistryInit/g) || []).length;
    if (commits !== 1) throw new Error(`RegistryInit закомічено ${commits} разів, очікував 1`);
  });

  await check('tempo від p1 доїхав до p2', async () => {
    l1.stdin.write('tempo 128\n');
    await waitFor(l2, /<- #\d+ TempoSet \{"bpm":128\}/);
  });

  await check('clip launch від p1 доїхав до p2', async () => {
    l1.stdin.write('launch 1 2\n');
    await waitFor(l2, /<- #\d+ ClipLaunch .*"track":\{"id":"[0-9a-f]{12}"/);
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
    const from = l2.out.length;
    l1.stdin.write('scene 3\n');
    await waitFor(l2, /<- #\d+ SceneLaunch \{"scene":\{"id":"[0-9a-f]{12}"\}\}/, 8000, from);
    if (/<- #\d+ ClipLaunch/.test(l2.out.slice(from))) {
      throw new Error('сцена розсипалась на окремі ClipLaunch');
    }
  });

  await check('stop all доїхав як одна подія', async () => {
    l1.stdin.write('stopall\n');
    await waitFor(l2, /<- #\d+ StopAllClips/);
    if (l2.out.match(/<- #\d+ ClipStop/g)) throw new Error('стоп розсипався на окремі ClipStop');
  });

  await check('новий MIDI clip і нота доїхали до партнера', async () => {
    const from = l2.out.length;
    l1.stdin.write('note 0 0 60 0 1 108\n');
    await waitFor(l2, /<- #\d+ ClipCreate .*"length":4/, 8000, from);
    await waitFor(l2, /<- #\d+ ClipNotesSet .*"pitch":60.*"velocity":108/, 8000, from);
    l1.stdin.write('note 0 0 72 5 0.5 96\n');
    await waitFor(l2, /<- #\d+ ClipNotesSet .*"pitch":72.*"start_time":5/, 8000, from);

    const stateFrom = l2.out.length;
    l2.stdin.write('state\n');
    await waitFor(l2, /"pitch": 60/, 8000, stateFrom);
    if (!/"velocity": 108/.test(l2.out.slice(stateFrom)) ||
        !/"pitch": 72/.test(l2.out.slice(stateFrom))) {
      throw new Error('стан MIDI-ноти у партнера не збігається');
    }
  });

  await check('видалення ноти очищає її регіон і не чіпає сусідній', async () => {
    const from = l1.out.length;
    l2.stdin.write('delnote 0 0 60 0\n');
    await waitFor(l1, /<- #\d+ ClipNotesSet .*"notes":\[\]/, 8000, from);

    const stateFrom = l1.out.length;
    l1.stdin.write('state\n');
    await waitFor(l1, /"pitch": 72/, 8000, stateFrom);
    if (/"pitch": 60/.test(l1.out.slice(stateFrom))) {
      throw new Error('видалена нота лишилась у стані партнера');
    }
  });

  await check('видалення MIDI clip доїхало до партнера', async () => {
    const from = l1.out.length;
    l2.stdin.write('delclip 0 0\n');
    await waitFor(l1, /<- #\d+ ClipDelete/, 8000, from);

    const stateFrom = l1.out.length;
    l1.stdin.write('state\n');
    await waitFor(l1, /"clips": \[\s*null/, 8000, stateFrom);
  });

  await check('uuid переживає переставляння треків у партнера', async () => {
    // p2 тасує свої треки: індекси поїхали, uuid лишились. Подія від p1,
    // адресована за uuid, має потрапити в той самий трек, а не в позицію.
    const fromL2 = l2.out.length;
    l2.stdin.write('move 0 2\n');
    await waitFor(l2, /id незмінний/, 8000, fromL2);

    // зсуви знімаємо ДО відправки, інакше подія може прилетіти раніше заміру
    const fromL1 = l1.out.length;
    const beforeApply = l2.out.length;
    l1.stdin.write('launch 0 5\n');
    await waitFor(l1, /-> ClipLaunch/, 8000, fromL1);
    const id = l1.out.slice(fromL1).match(/-> ClipLaunch \{"track":\{"id":"([0-9a-f]{12})"/)?.[1];
    if (!id) throw new Error('не вдалось витягти uuid треку з події p1');

    await waitFor(l2, new RegExp(`<- #\\d+ ClipLaunch .*"id":"${id}"`), 8000, beforeApply);
    if (/ВІДХИЛЕНО/.test(l2.out.slice(beforeApply))) throw new Error('подію відхилено після переставляння');

    l2.stdin.write('state\n');
    await new Promise((r) => setTimeout(r, 300));
    const hit = new RegExp(`"id": "${id}",\\s*"name": "[^"]*",\\s*"playing_slot_index": 5`);
    if (!hit.test(l2.out)) throw new Error('кліп поїхав не в той трек');
  });

  await check('старіший учасник виявляється при конекті, а не за симптомами', async () => {
    // bridge, що вдає версію 0.7.0: знає лише транспорт і темп
    const lOld = launch('live-old', join(root, 'daemon/tools/fake-live.js'), [
      '--udp-in', '19955', '--udp-out', '19956',
      '--script', '0.7.0-fake', '--events', 'TransportSet,TempoSet',
    ], { cwd: join(root, 'daemon') });
    await waitFor(lOld, /слухаю :19956/);
    const dOld = launch('daemon-p5', join(root, 'daemon/index.js'), [
      '--author', 'p5', '--session', SESSION,
      '--relay', `ws://127.0.0.1:${RELAY_PORT}`,
      '--udp-in', '19955', '--udp-out', '19956',
      '--state-dir', tmp, '--project', projectOf('p1'),
    ], { cwd: join(root, 'daemon') });

    await waitFor(dOld, /НЕСУМІСНІСТЬ.*не вміє застосовувати.*MixerSet/, 15000);
  });

  await check('мікшер: гучність доїхала до партнера', async () => {
    const from = l2.out.length;
    l1.stdin.write('vol 1 0.62\n');
    await waitFor(l2, /<- #\d+ MixerSet .*"param":"volume","value":0\.62/, 8000, from);
  });

  await check('мікшер: send адресується індексом', async () => {
    const from = l2.out.length;
    l1.stdin.write('send 1 0 0.25\n');
    await waitFor(l2, /<- #\d+ MixerSet .*"param":"send","value":0\.25,"index":0/, 8000, from);
  });

  await check('mute доїхав окремим типом події', async () => {
    const from = l2.out.length;
    l1.stdin.write('mute 2\n');
    await waitFor(l2, /<- #\d+ TrackToggle .*"param":"mute","value":true/, 8000, from);
  });

  await check('device parameter адресується сигнатурою та ordinal дубліката', async () => {
    const from = l2.out.length;
    // device 2 is the second of two identical Auto Filters: ordinal must be 1.
    l1.stdin.write('device 0 2 0 0.83\n');
    await waitFor(l2, /<- #\d+ DeviceParamSet .*"class_name":"AutoFilter".*"ordinal":1.*"name":"Frequency".*"value":0\.83/, 8000, from);

    const stateFrom = l2.out.length;
    l2.stdin.write('state\n');
    await waitFor(l2, /"value": 0\.83/, 8000, stateFrom);
  });

  await check('quantized device parameter синхронізується у зворотний бік', async () => {
    const from = l1.out.length;
    l2.stdin.write('device 1 0 0 0\n');
    await waitFor(l1, /<- #\d+ DeviceParamSet .*"class_name":"Operator".*"name":"Device On".*"value":0/, 8000, from);
  });

  let newTrackId = null;

  await check('створення треку доїхало до партнера', async () => {
    const from = l2.out.length;
    l1.stdin.write('addtrack midi\n');
    await waitFor(l2, /<- #\d+ TrackCreate/, 8000, from);
    newTrackId = l2.out.slice(from).match(/TrackCreate \{"track":\{"id":"([0-9a-f]{12})"/)?.[1];
    if (!newTrackId) throw new Error('не вдалось витягти uuid нового треку');
  });

  await check('на новостворений трек одразу можна запустити кліп', async () => {
    // реєстр лишається живим після бутстрапу, а не застигає на ньому
    const from = l2.out.length;
    l1.stdin.write('launch 3 1\n');
    await waitFor(l2, new RegExp(`<- #\\d+ ClipLaunch .*"id":"${newTrackId}"`), 8000, from);
    if (/ВІДХИЛЕНО/.test(l2.out.slice(from))) throw new Error('подію на новий трек відхилено');
  });

  await check('видалення треку доїхало, повторне застосування безпечне', async () => {
    const from = l2.out.length;
    l1.stdin.write('deltrack 3\n');
    await waitFor(l2, /<- #\d+ TrackDelete/, 8000, from);
    // зріз беремо ПІСЛЯ рядка TrackDelete -- він сам містить цей uuid
    const afterDelete = l2.out.length;
    l2.stdin.write('state\n');
    await new Promise((r) => setTimeout(r, 300));
    if (l2.out.slice(afterDelete).includes(newTrackId)) {
      throw new Error('трек лишився у партнера після видалення');
    }
  });

  await check('семпл, покладений у p1, доїхав до p2 байт у байт', async () => {
    // 400 КБ -- більше за CHUNK, тож збірка з кількох частин теж перевіряється
    const blob = Buffer.alloc(400 * 1024);
    for (let i = 0; i < blob.length; i++) blob[i] = (i * 7 + 13) & 0xff;
    writeFileSync(join(tmp, 'project-p1', 'Samples', 'kick.wav'), blob);

    await waitFor(d2, /filesync: отримав Samples\/kick\.wav/, 25000);
    const got = readFileSync(join(tmp, 'project-p2', 'Samples', 'kick.wav'));
    if (!got.equals(blob)) throw new Error(`файл побився: ${got.length} Б замість ${blob.length}`);
  });

  await check('передача файлів не потрапляє в журнал', async () => {
    const lines = readFileSync(join(tmp, `${SESSION}.jsonl`), 'utf8');
    if (/file_chunk|files_manifest|kick\.wav/.test(lines)) {
      throw new Error('File Sync Layer протік у event log');
    }
  });

  await check('журнал: 20 подій, монотонний gseq, цілий hash-chain', async () => {
    await new Promise((r) => setTimeout(r, 400));
    const lines = readFileSync(join(tmp, `${SESSION}.jsonl`), 'utf8').split('\n').filter(Boolean);
    if (lines.length !== 20) throw new Error(`очікував 20 подій, у журналі ${lines.length}`);
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

  await check('той самий автор в іншій сесії не глухий до неї', async () => {
    // gseq нумерується в межах сесії. Якщо стан daemon зберігати лише за автором,
    // lastGseq зі старої сесії відкине всі події нової як «вже бачені».
    const lOther = mkLive(4, 19951, 19952);
    await waitFor(lOther, /слухаю :19952/);
    const dOther = launch('daemon-p1-other', join(root, 'daemon/index.js'), [
      '--author', 'p1', '--session', 'other',
      '--relay', `ws://127.0.0.1:${RELAY_PORT}`,
      '--udp-in', '19951', '--udp-out', '19952',
      '--state-dir', tmp, '--project', projectOf('p1'),
    ], { cwd: join(root, 'daemon') });

    await waitFor(dOther, /#1 RegistryInit/, 15000);
    await waitFor(lOther, /реєстр прийнято/, 15000);
  });

  await check('хвіст журналу не втрачається, поки bridge мовчить', async () => {
    // daemon стартує в сесію з готовим журналом, а fake-live підіймається пізніше:
    // події мають дочекатись його, а не зникнути разом із просунутим lastGseq
    const dLater = launch('daemon-p4', join(root, 'daemon/index.js'), [
      '--author', 'p4', '--session', SESSION,
      '--relay', `ws://127.0.0.1:${RELAY_PORT}`,
      '--udp-in', '19953', '--udp-out', '19954',
      '--state-dir', tmp, '--project', projectOf('p1'),
    ], { cwd: join(root, 'daemon') });
    await waitFor(dLater, /relay: head=/, 10000);

    const lLater = mkLive(5, 19953, 19954);
    await waitFor(dLater, /застосовую \d+ відкладених подій/, 15000);
    await waitFor(lLater, /<- #\d+ TempoSet/, 10000);
  });

  await check('подія на невідомий uuid відхиляється, а не застосовується не туди', async () => {
    const before = l2.out.length;
    await new Promise((resolve, reject) => {
      const ws = new WebSocket(`ws://127.0.0.1:${RELAY_PORT}`);
      ws.on('open', () => ws.send(JSON.stringify({ m: 'join', session: SESSION, author: 'ghost', since: 1e9, proto: 1 })));
      ws.on('message', (raw) => {
        const msg = JSON.parse(raw);
        if (msg.m === 'welcome') {
          ws.send(JSON.stringify({
            m: 'submit',
            event: { type: 'ClipStop', payload: { track: { id: 'deadbeefcafe' } }, lseq: Date.now() },
          }));
        } else if (msg.m === 'commit' && msg.event.author === 'ghost') {
          ws.close();
          resolve();
        }
      });
      ws.on('error', reject);
    });
    await waitFor(l2, /ВІДХИЛЕНО \(невідомий трек\)/, 4000);
    if (!/ВІДХИЛЕНО/.test(l2.out.slice(before))) throw new Error('подію не відхилено');
  });
} catch (e) {
  failed += 1;
  console.log(`\nсценарій обірвався: ${e.message}`);
} finally {
  cleanup();
}

console.log(failed ? `\n${failed} перевірок впало` : '\nусі перевірки пройшли');
process.exit(failed ? 1 : 0);
