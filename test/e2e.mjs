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

/** Подія від стороннього клієнта. Єдиний спосіб перевірити прийом того, чого
 *  локальний bridge ніколи не надішле: адреси без обʼєкта, чужі uri, старі поля. */
const inject = (event) => new Promise((resolve, reject) => {
  const ws = new WebSocket(`ws://127.0.0.1:${RELAY_PORT}`);
  ws.on('open', () => ws.send(JSON.stringify({
    m: 'join', session: SESSION, author: 'ghost', since: 1e9, proto: 1,
  })));
  ws.on('message', (raw) => {
    const msg = JSON.parse(raw);
    if (msg.m === 'welcome') {
      ws.send(JSON.stringify({ m: 'submit', event: { ...event, lseq: Date.now() } }));
    } else if (msg.m === 'commit' && msg.event.author === 'ghost') {
      ws.close();
      resolve();
    }
  });
  ws.on('error', reject);
});

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

  const mkLive = (n, inPort, outPort, author = 'p1') =>
    launch(`live-${n}`, join(root, 'daemon/tools/fake-live.js'),
      ['--udp-in', String(inPort), '--udp-out', String(outPort),
       '--project', projectOf(author)], { cwd: join(root, 'daemon') });

  const l1 = mkLive(1, 19945, 19946);
  const l2 = mkLive(2, 19947, 19948, 'p2');
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

  await check('Track metadata: назва і колір синхронізуються в обох напрямках', async () => {
    let from = l2.out.length;
    l1.stdin.write('meta track:1 name Bass Lead\n');
    await waitFor(l2, /<- #\d+ ObjectMetaSet .*"object":"track".*"prop":"name","value":"Bass Lead"/, 8000, from);
    from = l1.out.length;
    l2.stdin.write('meta track:1 color 1122867\n');
    await waitFor(l1, /<- #\d+ ObjectMetaSet .*"object":"track".*"prop":"color","value":1122867/, 8000, from);
  });

  await check('Return metadata адресується aux UUID', async () => {
    const from = l2.out.length;
    l1.stdin.write('meta return:0 name Shared Return\n');
    await waitFor(l2, /<- #\d+ ObjectMetaSet .*"kind":"return".*"prop":"name","value":"Shared Return"/, 8000, from);
    l1.stdin.write('meta return:0 color 4478310\n');
    await waitFor(l2, /<- #\d+ ObjectMetaSet .*"kind":"return".*"prop":"color","value":4478310/, 8000, from);
  });

  await check('Master metadata синхронізується у зворотний бік', async () => {
    const from = l1.out.length;
    l2.stdin.write('meta master name Shared Master\n');
    await waitFor(l1, /<- #\d+ ObjectMetaSet .*"kind":"master".*"prop":"name","value":"Shared Master"/, 8000, from);
    l2.stdin.write('meta master color 7833753\n');
    await waitFor(l1, /<- #\d+ ObjectMetaSet .*"kind":"master".*"prop":"color","value":7833753/, 8000, from);
  });

  await check('Scene metadata синхронізується UUID-адресою', async () => {
    const from = l2.out.length;
    l1.stdin.write('meta scene:2 name Drop\n');
    await waitFor(l2, /<- #\d+ ObjectMetaSet .*"object":"scene".*"prop":"name","value":"Drop"/, 8000, from);
    l1.stdin.write('meta scene:2 color 10053171\n');
    await waitFor(l2, /<- #\d+ ObjectMetaSet .*"object":"scene".*"prop":"color","value":10053171/, 8000, from);
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

  await check('Session Clip metadata синхронізується через Track+Scene UUID', async () => {
    const from = l1.out.length;
    l2.stdin.write('meta clip:0:0 name Shared Clip\n');
    await waitFor(l1, /<- #\d+ ObjectMetaSet .*"object":"clip".*"prop":"name","value":"Shared Clip"/, 8000, from);
    l2.stdin.write('meta clip:0:0 color 16755200\n');
    await waitFor(l1, /<- #\d+ ObjectMetaSet .*"object":"clip".*"prop":"color","value":16755200/, 8000, from);
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
    const hit = new RegExp(`"id": "${id}",\\s*"name": "[^"]*",(?:\\s*"color": \\d+,)?\\s*"playing_slot_index": 5`);
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

  await check('партнер бачить, хто зараз редагує, і лок сам відпускається', async () => {
    // Лок бере daemon на першу подію жесту і знімає після паузи в потоці.
    // Спершу дочекаємось, поки відпустить попередній рух фейдера, інакше
    // новий жест лише поновить наявний лок і партнер нічого не побачить.
    const from = d2.out.length;
    l1.stdin.write('vol 1 0.55\n');
    await waitFor(d2, /ніхто нічого не редагує/, 8000, from);

    const gesture = d2.out.length;
    l1.stdin.write('vol 1 0.44\n');
    await waitFor(d2, /редагують: p1 — /, 8000, gesture);
    await waitFor(d2, /ніхто нічого не редагує/, 8000, gesture);
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

  await check('Return mixer: volume і pan синхронізуються з aux UUID', async () => {
    const from = l2.out.length;
    l1.stdin.write('mix return:0 volume 0.44\n');
    await waitFor(l2, /<- #\d+ MixerSet .*"kind":"return".*"param":"volume","value":0\.44/, 8000, from);
    l1.stdin.write('mix return:0 panning 0.31\n');
    await waitFor(l2, /<- #\d+ MixerSet .*"kind":"return".*"param":"panning","value":0\.31/, 8000, from);
  });

  await check('Return mixer: mute і solo синхронізуються у зворотний бік', async () => {
    const from = l1.out.length;
    l2.stdin.write('toggle return:1 mute\n');
    await waitFor(l1, /<- #\d+ TrackToggle .*"kind":"return".*"param":"mute","value":true/, 8000, from);
    l2.stdin.write('toggle return:1 solo\n');
    await waitFor(l1, /<- #\d+ TrackToggle .*"kind":"return".*"param":"solo","value":true/, 8000, from);
  });

  await check('Master mixer: volume, pan і crossfader синхронізуються', async () => {
    const from = l2.out.length;
    l1.stdin.write('mix master volume 0.72\n');
    await waitFor(l2, /<- #\d+ MixerSet .*"kind":"master".*"param":"volume","value":0\.72/, 8000, from);
    l1.stdin.write('mix master panning 0.58\n');
    await waitFor(l2, /<- #\d+ MixerSet .*"kind":"master".*"param":"panning","value":0\.58/, 8000, from);
    l1.stdin.write('mix master crossfader 0.81\n');
    await waitFor(l2, /<- #\d+ MixerSet .*"kind":"master".*"param":"crossfader","value":0\.81/, 8000, from);
  });

  await check('Master mixer: cue volume синхронізується у зворотний бік', async () => {
    const from = l1.out.length;
    l2.stdin.write('mix master cue_volume 0.36\n');
    await waitFor(l1, /<- #\d+ MixerSet .*"kind":"master".*"param":"cue_volume","value":0\.36/, 8000, from);
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

  await check('параметр device проходить через два вкладені Rack chains', async () => {
    const from = l2.out.length;
    // device path: outer Rack / duplicate chain #1 / inner Rack / chain #0 / Auto Filter.
    l1.stdin.write('device 0 3/1/0/0/0 0 0.91\n');
    await waitFor(l2, /<- #\d+ DeviceParamSet .*"class_name":"AutoFilter".*"value":0\.91,"chain_path":\[\{"id":"[0-9a-f]{12}"\},\{"id":"[0-9a-f]{12}"\}\]/, 8000, from);
  });

  await check('UUID Rack chain працює у зворотний бік для chain-тезок', async () => {
    const from = l1.out.length;
    // Outer chain #0 has the same name as #1, but a distinct UUID.
    l2.stdin.write('device 2 3/0/0 0 0.12\n');
    await waitFor(l1, /<- #\d+ DeviceParamSet .*"class_name":"AutoFilter".*"value":0\.12,"chain_path":\[\{"id":"[0-9a-f]{12}"\}\]/, 8000, from);
  });

  await check('параметр device на Return Track доходить до партнера', async () => {
    const from = l2.out.length;
    l1.stdin.write('device return:0 0 0 0.44\n');
    await waitFor(l2, /<- #\d+ DeviceParamSet \{"track":\{"id":"[0-9a-f]{12}","kind":"return"\}.*"class_name":"AutoFilter".*"value":0\.44/, 8000, from);
  });

  await check('параметр device на Master Track синхронізується у зворотний бік', async () => {
    const from = l1.out.length;
    l2.stdin.write('device master 0 0 0.88\n');
    await waitFor(l1, /<- #\d+ DeviceParamSet \{"track":\{"id":"[0-9a-f]{12}","kind":"master"\}.*"class_name":"AutoFilter".*"value":0\.88/, 8000, from);
  });

  await check('Return Track зберігає aux identity через вкладені Rack chains', async () => {
    const from = l2.out.length;
    // Return 0: outer Rack #1 / duplicate chain #1 / inner Rack #0 / chain #0 / filter #0.
    l1.stdin.write('device return:0 1/1/0/0/0 0 0.73\n');
    await waitFor(l2, /<- #\d+ DeviceParamSet \{"track":\{"id":"[0-9a-f]{12}","kind":"return"\}.*"value":0\.73,"chain_path":\[\{"id":"[0-9a-f]{12}"\},\{"id":"[0-9a-f]{12}"\}\]/, 8000, from);
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

  await check('bridge віддає повний стан сету, daemon збирає його з чанків', async () => {
    // Кліп із нотою навмисно створюємо перед знімком: попередні перевірки свій
    // кліп уже видалили, і без цього нотна гілка серіалізатора лишилась би сліпою
    const notesFrom = l2.out.length;
    l1.stdin.write('note 1 1 64 0 1 100\n');
    await waitFor(l2, /<- #\d+ ClipNotesSet .*"pitch":64/, 8000, notesFrom);
    const from = d1.out.length;
    l1.stdin.write('fullstate\n');
    await waitFor(d1, /state: знімок \d+ зібрано/, 10000, from);

    const state = JSON.parse(readFileSync(join(tmp, 'p1.e2e.state.json'), 'utf8'));
    if (state.version !== 1) throw new Error(`версія знімка ${state.version}`);

    const named = state.tracks.find((t) => t.name === 'Bass Lead');
    if (!named) throw new Error('у знімку немає перейменованого треку');
    if (!named.devices.length || !named.devices[0].parameters.length) {
      throw new Error('девайси треку не потрапили у знімок');
    }

    // Вкладений Rack: без chain_path девайс усередині chain неадресовний
    const nested = state.tracks.concat(state.aux_tracks)
      .flatMap((t) => t.devices).find((d) => d.chain_path?.length);
    if (!nested) throw new Error('девайси всередині Rack chains не потрапили у знімок');

    const master = state.aux_tracks.find((t) => t.kind === 'master');
    if (master?.mixer?.volume !== 0.72) {
      throw new Error(`гучність Master у знімку ${master?.mixer?.volume} замість 0.72`);
    }
    const withClip = state.tracks.find((t) => t.clips.length);
    if (!withClip) throw new Error('Session-кліпи не потрапили у знімок');
    const noted = withClip.clips.find((c) => (c.notes || []).some((n) => n.pitch === 64));
    if (!noted) throw new Error('ноти кліпу не потрапили у знімок');
    if (!noted.scene?.id || !noted.clip?.length) {
      throw new Error('кліп у знімку без адреси сцени або довжини');
    }

    if (!state.scenes.length || !state.scenes.every((s) => s.id)) {
      throw new Error('сцени у знімку без uuid');
    }
  });

  await check('чужий знімок застосовується локально і не породжує подій', async () => {
    // Беремо знімок p1, правимо в ньому значення -- і згодовуємо назад. Так
    // виглядає приєднання до сету, який бачиш уперше: адреси ті самі, значення чужі.
    const source = JSON.parse(readFileSync(join(tmp, 'p1.e2e.state.json'), 'utf8'));
    const master = source.aux_tracks.find((t) => t.kind === 'master');
    master.mixer.volume = 0.33;
    const track = source.tracks.find((t) => t.name === 'Bass Lead');
    track.name = 'Adopted';
    const clip = track.clips.find((c) => (c.notes || []).length);
    clip.notes = [{ pitch: 48, start_time: 0, duration: 0.5, velocity: 77, mute: false }];
    const foreign = join(tmp, 'foreign-state.json');
    writeFileSync(foreign, JSON.stringify(source));

    const headBefore = relay.out.match(/#(\d+) /g)?.length ?? 0;
    const from = d1.out.length;
    d1.stdin.write(`apply ${foreign}\n`);
    await waitFor(d1, /знімок застосовано: \d+ з \d+$/m, 15000, from);
    if (/знімок застосовано.*помилок/.test(d1.out.slice(from))) {
      throw new Error(`застосування з помилками: ${d1.out.slice(from).split('\n').find((l) => /застосовано/.test(l))}`);
    }

    const stateFrom = d1.out.length;
    l1.stdin.write('fullstate\n');
    await waitFor(d1, /state: знімок \d+ зібрано/, 10000, stateFrom);
    const after = JSON.parse(readFileSync(join(tmp, 'p1.e2e.state.json'), 'utf8'));

    const masterAfter = after.aux_tracks.find((t) => t.kind === 'master');
    if (masterAfter.mixer.volume !== 0.33) {
      throw new Error(`гучність Master ${masterAfter.mixer.volume} замість 0.33`);
    }
    if (!after.tracks.some((t) => t.name === 'Adopted')) {
      throw new Error('назва треку зі знімка не застосувалась');
    }
    const notesAfter = after.tracks.flatMap((t) => t.clips).flatMap((c) => c.notes || []);
    if (!notesAfter.some((n) => n.pitch === 48 && n.velocity === 77)) {
      throw new Error('ноти зі знімка не застосувались');
    }
    if (notesAfter.some((n) => n.pitch === 64)) {
      throw new Error('регіон знімка не прибрав стару ноту');
    }

    // Найважливіше: локальне вирівнювання не є подією і в журнал не потрапляє
    const headAfter = relay.out.match(/#(\d+) /g)?.length ?? 0;
    if (headAfter !== headBefore) {
      throw new Error(`застосування знімка породило ${headAfter - headBefore} подій у журналі`);
    }
  });

  await check('знімок із чужою структурою каже, чого саме бракує', async () => {
    // Так виглядає партнер, у якого інший набір девайсів: адреси є, обʼєктів немає
    const source = JSON.parse(readFileSync(join(tmp, 'p1.e2e.state.json'), 'utf8'));
    source.tracks.push({
      id: 'deadbeefcafe', idx: 9, name: 'Ghost', color: 0, kind: 'midi',
      mixer: { volume: 0.5 }, devices: [], clips: [],
    });
    const victim = source.tracks[0];
    victim.devices.push({
      device: { class_name: 'Serum', class_display_name: 'Serum', ordinal: 0 },
      parameters: [{ name: 'Cutoff', ordinal: 0, value: 0.4 }, { name: 'Res', ordinal: 0, value: 0.2 }],
    });
    victim.devices[0].parameters.push({ name: 'НемаТакого', ordinal: 0, value: 0.1 });
    const foreign = join(tmp, 'gappy-state.json');
    writeFileSync(foreign, JSON.stringify(source));

    const from = d1.out.length;
    d1.stdin.write(`apply ${foreign}\n`);
    await waitFor(d1, /знімок застосовано: \d+ з \d+, \d+ пропущено\. Бракує ось чого:/, 15000, from);
    const said = d1.out.slice(from);

    if (!/трек deadbeefcafe — такого немає/.test(said)) throw new Error('не назвав відсутній трек');
    if (!/Serum — немає девайса \(2 значень\)/.test(said)) throw new Error('не назвав відсутній девайс');
    if (!/немає параметра НемаТакого/.test(said)) throw new Error('не назвав відсутній параметр');

    const listed = JSON.parse(readFileSync(join(tmp, 'p1.e2e.missing.json'), 'utf8'));
    if (!listed.missing.some((g) => g.what === 'device' && g.count === 2)) {
      throw new Error('у файлі прогалин немає згорнутого девайса');
    }
  });

  await check('знімок їде до партнера через relay і вирівнює його сет', async () => {
    // Попередня перевірка навмисно розвела машини: p1 застосував чужі значення
    // локально, і p2 про них не знає -- бо вирівнювання не породжує подій
    const headBefore = (relay.out.match(/#\d+ /g) || []).length;
    const from = d2.out.length;
    d2.stdin.write('pull p1\n');

    await waitFor(d1, /p2 просить знімок стану/, 8000);
    await waitFor(d2, /знімок p1 отримано/, 15000, from);
    await waitFor(d2, /знімок застосовано: \d+ з \d+$/m, 15000, from);
    if (/знімок застосовано.*помилок/.test(d2.out.slice(from))) {
      throw new Error(d2.out.slice(from).split('\n').find((l) => /застосовано/.test(l)));
    }

    const stateFrom = d2.out.length;
    l2.stdin.write('fullstate\n');
    await waitFor(d2, /state: знімок \d+ зібрано/, 10000, stateFrom);
    const after = JSON.parse(readFileSync(join(tmp, 'p2.e2e.state.json'), 'utf8'));

    const master = after.aux_tracks.find((t) => t.kind === 'master');
    if (master.mixer.volume !== 0.33) {
      throw new Error(`p2 не підхопив гучність Master: ${master.mixer.volume}`);
    }
    if (!after.tracks.some((t) => t.name === 'Adopted')) {
      throw new Error('p2 не підхопив назву треку зі знімка');
    }

    const headAfter = (relay.out.match(/#\d+ /g) || []).length;
    if (headAfter !== headBefore) {
      throw new Error(`обмін знімками породив ${headAfter - headBefore} подій у журналі`);
    }
  });

  await check('партнер бачить, на що я дивлюсь, і пізній учасник теж', async () => {
    const from = d2.out.length;
    l1.stdin.write('look 1 2\n');
    await waitFor(d2, /дивляться: p1: /, 8000, from);

    // Присутність — стан на relay, а не труба: той, хто прийшов пізніше,
    // бачить її вже у welcome, не чекаючи наступного руху партнера
    const welcome = await new Promise((resolve, reject) => {
      const ws = new WebSocket(`ws://127.0.0.1:${RELAY_PORT}`);
      ws.on('open', () => ws.send(JSON.stringify({
        m: 'join', session: SESSION, author: 'watcher', since: 1e9, proto: 1,
      })));
      ws.on('message', (raw) => {
        const msg = JSON.parse(raw);
        if (msg.m !== 'welcome') return;
        ws.close();
        resolve(msg);
      });
      ws.on('error', reject);
    });
    const seen = (welcome.presence || []).find((entry) => entry.author === 'p1');
    if (!seen?.view?.track?.id) throw new Error('присутності p1 немає у welcome');
    if (!seen.view.names?.track) throw new Error('присутність без людської назви');
  });

  await check('follow веде мій вид за партнером і не відбивається назад', async () => {
    const from = d2.out.length;
    d2.stdin.write('follow p1\n');
    await waitFor(d2, /слідую за p1/, 5000, from);

    const liveFrom = l2.out.length;
    l1.stdin.write('look 2 3\n');
    await waitFor(l2, /<- view_set від p1/, 8000, liveFrom);
    if (/-> view/.test(l2.out.slice(liveFrom))) {
      throw new Error('чужий вид відбився назад — це і є ping-pong');
    }
    d2.stdin.write('follow off\n');
    await waitFor(d2, /більше не слідую/, 5000, from);
  });

  await check('взаємний follow відхиляється', async () => {
    l2.stdin.write('look 0 0\n');
    const from = d2.out.length;
    d2.stdin.write('follow p1\n');
    await waitFor(d2, /слідую за p1/, 5000, from);

    const mine = d1.out.length;
    d1.stdin.write('follow p2\n');
    await waitFor(d1, /follow: p2 уже слідує за тобою/, 8000, mine);
    d1.stdin.write('follow off\n');
    d2.stdin.write('follow off\n');
  });

  await check('присутність не потрапляє в журнал', async () => {
    const lines = readFileSync(join(tmp, `${SESSION}.jsonl`), 'utf8');
    if (/"presence"|"view"|view_set/.test(lines)) {
      throw new Error('погляд протік у event log');
    }
  });

  await check('партнер відкочує чужу зміну, і це звичайна подія в журналі', async () => {
    const seen = l2.out.length;
    l1.stdin.write('vol 1 0.11\n');
    await waitFor(l2, /<- #\d+ MixerSet .*"value":0\.11/, 8000, seen);
    l1.stdin.write('vol 1 0.77\n');
    await waitFor(l2, /<- #\d+ MixerSet .*"value":0\.77/, 8000, seen);

    // Відкочує ПАРТНЕР, а не автор: у цьому й сенс undo в мультиплеєрі
    const back = l1.out.length;
    const from = d2.out.length;
    d2.stdin.write('undo p1\n');
    await waitFor(d2, /відкочую MixerSet від p1/, 8000, from);
    // Значення повертається на машині автора -- як подія від p2
    await waitFor(l1, /<- #\d+ MixerSet .*"value":0\.11/, 8000, back);

    const from2 = d2.out.length;
    d2.stdin.write('undo p9\n');
    await waitFor(d2, /undo неможливий: у p9 немає дій/, 8000, from2);
  });

  await check('межі кліпу їдуть однією подією, з усіма пʼятьма полями', async () => {
    // Кліп під петлю робимо свій: попередні перевірки свої слоти вже прибрали
    const from = l2.out.length;
    l1.stdin.write('note 2 3 60 0 2 100\n');
    await waitFor(l2, /<- #\d+ ClipCreate/, 8000, from);

    const loopFrom = l2.out.length;
    l1.stdin.write('loop 2 3 1 3\n');
    await waitFor(l2, /<- #\d+ ClipLoopSet/, 8000, loopFrom);
    const seen = l2.out.slice(loopFrom);
    // Пʼять окремих подій Live клампив би одна об одну через невалідні
    // проміжні стани -- тому вони мусять приїхати разом
    for (const [prop, want] of [['looping', 'true'], ['loop_start', '1'],
      ['loop_end', '3'], ['start_marker', '1'], ['end_marker', '3']]) {
      if (!new RegExp(`"${prop}":${want}`).test(seen)) {
        throw new Error(`${prop} не приїхав однією подією з рештою`);
      }
    }
    if (/ClipLoopSet ВІДХИЛЕНО/.test(seen)) throw new Error('петлю відхилено');
  });

  await check('петля на порожньому слоті не створює кліп', async () => {
    const state = JSON.parse(readFileSync(join(tmp, 'p1.e2e.state.json'), 'utf8'));
    const track = state.tracks[0];
    const busy = new Set((track.clips || []).map((c) => c.scene?.id));
    const scene = [...state.scenes].reverse().find((s) => !busy.has(s.id));
    if (!scene) throw new Error('не знайшов порожнього слоту для перевірки');

    const from = l2.out.length;
    await inject({
      type: 'ClipLoopSet',
      payload: {
        track: { id: track.id }, scene: { id: scene.id },
        looping: true, loop_start: 0, loop_end: 4, start_marker: 0, end_marker: 4,
      },
    });
    await waitFor(l2, /ClipLoopSet ВІДХИЛЕНО \(кліпу немає\)/, 6000, from);
  });

  await check('копія треку приїжджає з девайсами, і ланцюги в ній сходяться самі', async () => {
    const from = l2.out.length;
    l1.stdin.write('duptrack 0\n');
    await waitFor(l2, /<- #\d+ TrackDuplicate/, 8000, from);
    if (/TrackDuplicate: джерела немає/.test(l2.out.slice(from))) {
      throw new Error('партнер не знайшов джерела і зробив порожній трек');
    }

    // Головна обіцянка стадії: ланцюги всередині Rack копії дістають ті самі
    // uuid на обох машинах без жодної нової події -- лише з локатора
    const paramFrom = l2.out.length;
    l1.stdin.write('device 1 3/0/0 0 0.91\n');
    await waitFor(l2, /<- #\d+ DeviceParamSet .*"value":0\.91/, 8000, paramFrom);
    const seen = l2.out.slice(paramFrom);
    if (!/"chain_path"/.test(seen)) throw new Error('подія приїхала без адреси ланцюга');
    if (/ВІДХИЛЕНО/.test(seen)) throw new Error('ланцюг у копії не розпізнався у партнера');
  });

  await check('девайс із браузера доїжджає; невідомий не підмінюється', async () => {
    const from = l2.out.length;
    l1.stdin.write('load 2 query:AudioFx#Compressor\n');
    await waitFor(l2, /<- #\d+ DeviceLoad .*Compressor/, 8000, from);
    if (/DeviceLoad ВІДХИЛЕНО/.test(l2.out.slice(from))) {
      throw new Error('партнер не знайшов девайс у своєму браузері');
    }

    const bad = l1.out.length;
    l1.stdin.write('load 2 query:AudioFx#Nope\n');
    await waitFor(l1, /немає девайса query:AudioFx#Nope/, 5000, bad);

    const state = JSON.parse(readFileSync(join(tmp, 'p1.e2e.state.json'), 'utf8'));
    const target = { id: state.tracks[0].id };

    // Замінник не створюємо ніколи: краще дірка, ніж чужий девайс на тій адресі
    const missing = l2.out.length;
    await inject({
      type: 'DeviceLoad',
      payload: { track: target, item: { uri: 'query:AudioFx#Ozone', name: 'Ozone', category: 'audio_effects' } },
    });
    await waitFor(l2, /DeviceLoad ВІДХИЛЕНО \(немає девайса Ozone\)/, 6000, missing);

    // uri зсувається між версіями Live -- назва лишається тим, що впізнає людина
    const byName = l2.out.length;
    await inject({
      type: 'DeviceLoad',
      payload: {
        track: target,
        item: { uri: 'query:AudioFx#Compressor2', name: 'Compressor', category: 'audio_effects' },
      },
    });
    await waitFor(l2, /<- #\d+ DeviceLoad .*Compressor2/, 6000, byName);
    if (/DeviceLoad ВІДХИЛЕНО/.test(l2.out.slice(byName))) {
      throw new Error('запасний пошук за назвою не спрацював');
    }
  });

  await check('партнер на старому скрипті не породжує ні фантома, ні кліпа-монстра', async () => {
    // Обидві події -- точні копії того, що прилетіло з машини на 0.17.0
    const state = JSON.parse(readFileSync(join(tmp, 'p1.e2e.state.json'), 'utf8'));

    const phantom = l2.out.length;
    await inject({
      type: 'TrackCreate',
      payload: { track: { id: 'aaaaaaaaaaaa', name: '1-Group' }, kind: 'group', idx: 0 },
    });
    await waitFor(l2, /TrackCreate ВІДХИЛЕНО \(невідомий різновид треку group\)/, 6000, phantom);

    // 63072000 доль -- заглушка Live для кліпу, що зараз записується
    const monster = l2.out.length;
    const track = state.tracks[0];
    const busy = new Set((track.clips || []).map((c) => c.scene?.id));
    const scene = [...state.scenes].reverse().find((s) => !busy.has(s.id));
    await inject({
      type: 'ClipCreate',
      payload: { track: { id: track.id }, scene: { id: scene.id }, clip: { length: 63072000 } },
    });
    await waitFor(l2, /<- #\d+ ClipCreate/, 6000, monster);

    const stateFrom = l2.out.length;
    l2.stdin.write('state\n');
    await waitFor(l2, /"tempo"/, 8000, stateFrom);
    if (/"length": 63072000/.test(l2.out.slice(stateFrom))) {
      throw new Error('довжина кліпу-монстра осіла в стані');
    }
  });

  await check('покладений девайс їде сам, і застосування не відбивається назад', async () => {
    const journal = () => readFileSync(join(tmp, `${SESSION}.jsonl`), 'utf8').split('\n').filter(Boolean).length;
    const before = journal();

    const from = l2.out.length;
    l1.stdin.write('adddevice 2 Compressor\n');
    await waitFor(l2, /<- #\d+ DeviceInsert .*"class_display_name":"Compressor"/, 8000, from);
    const seen = l2.out.slice(from);
    if (/"uri"/.test(seen)) throw new Error('DeviceInsert адресує назвою, а не uri з браузера');
    if (!/"index":\d+/.test(seen)) throw new Error('подія приїхала без позиції');
    if (/DeviceInsert ВІДХИЛЕНО/.test(seen)) throw new Error('партнер не прийняв автоподію');

    // Найтонше місце етапу 2: у партнера теж спрацює _on_devices, і без
    // глушіння він емітив би DeviceLoad назад -- по колу, без кінця.
    await new Promise((r) => setTimeout(r, 800));
    const grew = journal() - before;
    if (grew !== 1) throw new Error(`одна дія дала ${grew} подій -- застосування відбилось назад`);
  });

  await check('пресет не видається за голий девайс', async () => {
    const journal = () => readFileSync(join(tmp, `${SESSION}.jsonl`), 'utf8').split('\n').filter(Boolean).length;
    const before = journal();

    // Warm Bus має той самий class_name, що й голий Compressor. Мовчки віддати
    // партнеру дефолт було б гірше за дірку: він чув би не те, що автор.
    l1.stdin.write('adddevice 2 Compressor Warm Bus\n');
    await waitFor(l1, /поклав Warm Bus/, 5000);
    await new Promise((r) => setTimeout(r, 800));
    if (journal() !== before) throw new Error('пресет полетів партнеру як стоковий девайс');
  });

  await check('перший девайс на порожньому треку теж їде', async () => {
    // Найтонше місце діффу структури: контейнер БЕЗ девайсів мусить мати
    // запис у знімку. Інакше він не відрізняється від щойно створеного,
    // а той навмисно пропускається -- і перший девайс на треку мовчав би.
    const journal = () => readFileSync(join(tmp, `${SESSION}.jsonl`), 'utf8').split('\n').filter(Boolean).length;
    const before = journal();

    const made = l2.out.length;
    l1.stdin.write('addtrack midi 0\n');
    await waitFor(l2, /<- #\d+ TrackCreate/, 8000, made);

    const from = l2.out.length;
    l1.stdin.write('adddevice 0 Compressor\n');
    await waitFor(l2, /<- #\d+ DeviceInsert .*"class_display_name":"Compressor"/, 8000, from);
    if (/DeviceInsert ВІДХИЛЕНО/.test(l2.out.slice(from))) throw new Error('партнер не прийняв подію');

    await new Promise((r) => setTimeout(r, 600));
    const grew = journal() - before;
    if (grew !== 2) throw new Error(`очікував TrackCreate + DeviceInsert, отримав ${grew} подій`);
  });

  await check('знятий девайс зникає і в партнера', async () => {
    // До цього видалення події не мало взагалі: діфф бачив зникнення і мовчав.
    const journal = () => readFileSync(join(tmp, `${SESSION}.jsonl`), 'utf8').split('\n').filter(Boolean).length;

    // Свіжий трек, щоб індекси не залежали від того, що наклали попередні
    // перевірки: видалення адресується позицією, і чужий девайс усе змазав би.
    const made = l2.out.length;
    l1.stdin.write('addtrack midi 0\n');
    await waitFor(l2, /<- #\d+ TrackCreate/, 8000, made);

    const put = l2.out.length;
    l1.stdin.write('adddevice 0 Compressor\n');
    await waitFor(l2, /<- #\d+ DeviceInsert .*"class_display_name":"Compressor"/, 8000, put);

    const before = journal();
    const from = l2.out.length;
    l1.stdin.write('deldevice 0 0\n');
    await waitFor(l2, /<- #\d+ DeviceDelete .*"class_display_name":"Compressor"/, 8000, from);
    const seen = l2.out.slice(from);
    if (/DeviceDelete ВІДХИЛЕНО/.test(seen)) throw new Error('партнер не прийняв видалення');
    if (!/"name":"Compressor"/.test(seen)) throw new Error('видалення їде без сигнатури для звірки');

    await new Promise((r) => setTimeout(r, 800));
    const grew = journal() - before;
    if (grew !== 1) throw new Error(`одне видалення дало ${grew} подій`);
  });

  await check('переїзд девайса їде одним DeviceMove, а не парою', async () => {
    const journal = () => readFileSync(join(tmp, `${SESSION}.jsonl`), 'utf8').split('\n').filter(Boolean).length;

    const put = l2.out.length;
    l1.stdin.write('adddevice 3 Compressor\n');
    await waitFor(l2, /<- #\d+ DeviceInsert/, 8000, put);

    // Переїзд між треками -- саме той випадок, який раніше глушив емісію
    // цілком: "зникло там, зʼявилось тут" виглядало як дві незалежні зміни.
    const before = journal();
    const from = l2.out.length;
    l1.stdin.write('movedevice 3 0 2 0\n');
    await waitFor(l2, /<- #\d+ DeviceMove /, 8000, from);
    const seen = l2.out.slice(from);
    if (/DeviceMove ВІДХИЛЕНО/.test(seen)) throw new Error('партнер не прийняв переїзд');
    if (!/"from":/.test(seen) || !/"to":/.test(seen)) throw new Error('переїзд без адрес');

    await new Promise((r) => setTimeout(r, 800));
    const grew = journal() - before;
    if (grew !== 1) throw new Error(`переїзд дав ${grew} подій замість одного DeviceMove`);
  });

  await check('режим pair розкладає переїзд на дві події і губить значення', async () => {
    const journal = () => readFileSync(join(tmp, `${SESSION}.jsonl`), 'utf8').split('\n').filter(Boolean).length;

    l1.stdin.write('movemode pair\n');
    await waitFor(l1, /режим переїзду: pair/, 5000);

    const put = l2.out.length;
    l1.stdin.write('adddevice 2 Compressor\n');
    await waitFor(l2, /<- #\d+ DeviceInsert/, 8000, put);

    const before = journal();
    const from = l2.out.length;
    l1.stdin.write('movedevice 2 0 3 0\n');
    await waitFor(l2, /<- #\d+ DeviceDelete/, 8000, from);
    await waitFor(l2, /<- #\d+ DeviceInsert/, 8000, from);
    if (/DeviceMove/.test(l2.out.slice(from))) throw new Error('у режимі pair не має бути DeviceMove');

    await new Promise((r) => setTimeout(r, 800));
    const grew = journal() - before;
    if (grew !== 2) throw new Error(`pair мав дати дві події, дав ${grew}`);

    // Саме тут і живе ціна режиму: девайс у партнера перестворюється з нуля.
    // На живому 12.3.8 це виміряно як Frequency 0.25 -> 0.899657.
    l1.stdin.write('movemode move\n');
    await waitFor(l1, /режим переїзду: move/, 5000);
  });

  await check('за тим індексом інший девайс -- партнер не стирає його', async () => {
    // Індекс каже, що поїхало; сигнатура ловить те, що поїхало не те.
    // Без цієї звірки розбіжність станів стерла б партнеру чужий девайс мовчки.
    const state = JSON.parse(readFileSync(join(tmp, 'p1.e2e.state.json'), 'utf8'));
    const target = { id: state.tracks[2].id };

    const from = l2.out.length;
    await inject({
      type: 'DeviceDelete',
      payload: { track: target, index: 0,
                 device: { class_name: 'Ozone9', class_display_name: 'Ozone', name: 'Ozone' } },
    });
    await waitFor(l2, /DeviceDelete ВІДХИЛЕНО/, 6000, from);
  });

  await check('кліп в Arrangement доїжджає з нотами, переїжджає і зникає', async () => {
    const notes = l1.out.length;
    l1.stdin.write('note 2 4 60 0 2 100\n');
    await waitFor(l2, /<- #\d+ ClipNotesSet .*"pitch":60/, 8000, notes);

    // Назва -- частина події, а не косметика: без неї копія приїжджає
    // безіменною, і люди бачать різні лінійки.
    l1.stdin.write('meta clip:2:4 name Verse\n');
    await waitFor(l2, /<- #\d+ ObjectMetaSet .*Verse/, 8000, notes);

    const born = l2.out.length;
    l1.stdin.write('arr 2 4 8\n');
    await waitFor(l2, /<- #\d+ ArrangementClipCreate .*"start_time":8/, 8000, born);
    // Вміст іде окремою подією -- саме тому створення лишається маленьким
    await waitFor(l2, /<- #\d+ ArrangementClipNotesSet .*"pitch":60/, 8000, born);
    if (/Arrangement.* ВІДХИЛЕНО/.test(l2.out.slice(born))) throw new Error('партнер відхилив кліп');

    const moved = l2.out.length;
    l1.stdin.write('movearr 2 0 16\n');
    await waitFor(l2, /<- #\d+ ArrangementClipMove .*"start_time":16/, 8000, moved);

    // Стан партнера мусить збігтися: кліп на 16-й долі, з нотою всередині
    const shown = l2.out.length;
    l2.stdin.write('state\n');
    await waitFor(l2, /"tempo"/, 8000, shown);
    const state = l2.out.slice(shown);
    if (!/"start_time": 16/.test(state)) throw new Error('у партнера кліп не на 16-й долі');
    if (!/"name": "Verse"/.test(state)) throw new Error('назва кліпу не доїхала в Arrangement');

    const gone = l2.out.length;
    l1.stdin.write('delarr 2 0\n');
    await waitFor(l2, /<- #\d+ ArrangementClipDelete/, 8000, gone);
    if (/ArrangementClipDelete ВІДХИЛЕНО/.test(l2.out.slice(gone))) {
      throw new Error('видалення не застосувалось');
    }
  });

  await check('чужий кліп в Arrangement, якого в нас немає, називається вголос', async () => {
    // Подія могла не доїхати -- партнер був офлайн, гілка розійшлась.
    // Тоді єдине чесне -- не мовчати про різницю.
    const from = d2.out.length;
    l2.stdin.write('fullstate\n');
    await waitFor(d2, /state: знімок \d+ зібрано/, 10000, from);
    const source = JSON.parse(readFileSync(join(tmp, 'p2.e2e.state.json'), 'utf8'));
    const victim = source.tracks.find((t) => t.id);
    victim.arrangement = [{ id: 'aaaabbbbcccc', start_time: 32, end_time: 36, length: 4, name: 'Bridge' }];
    const foreign = join(tmp, 'arr-gap.json');
    writeFileSync(foreign, JSON.stringify(source));

    const said = d2.out.length;
    d2.stdin.write(`apply ${foreign}\n`);
    await waitFor(d2, /Бракує ось чого:/, 15000, said);
    if (!/кліп «Bridge» в Arrangement на 32-й долі є в партнера, у тебе немає/.test(d2.out.slice(said))) {
      throw new Error('про чужий Arrangement-кліп не сказано');
    }
  });

  await check('семпл у слоті доїжджає адресою, а не байтами', async () => {
    // Байти вже привіз filesync попередньою перевіркою -- kick.wav лежить
    // в обох проєктах. Подія несе лише шлях відносно теки проєкту: саме він
    // портативний, на відміну від абсолютного шляху й машинних FileId.
    const from = l2.out.length;
    l1.stdin.write('dropsample 2 5 Samples/kick.wav\n');
    await waitFor(l1, /поклав Samples\/kick\.wav у слот 5/, 8000);
    await waitFor(l2, /<- #\d+ SampleLoad .*"path":"Samples\/kick\.wav"/, 8000, from);
    const seen = l2.out.slice(from);
    if (/SampleLoad ВІДХИЛЕНО/.test(seen)) throw new Error('партнер відхилив семпл');

    const shown = l2.out.length;
    l2.stdin.write('state\n');
    await waitFor(l2, /"tempo"/, 8000, shown);
    const state = l2.out.slice(shown);
    if (!/"kind": "audio"/.test(state)) throw new Error('audio-кліп у партнера не створився');
    if (!/"file_path": "Samples\/kick\.wav"/.test(state)) {
      throw new Error('кліп створився без посилання на семпл');
    }
  });

  await check('подія без файлу не вигадує кліп', async () => {
    // Подія цілком може випередити filesync. Чесна відмова краща за порожній
    // кліп: у bridge на цей випадок черга з очікуванням, тут -- ВІДХИЛЕНО.
    const state = JSON.parse(readFileSync(join(tmp, 'p1.e2e.state.json'), 'utf8'));
    const track = state.tracks[0];
    const busy = new Set((track.clips || []).map((c) => c.scene?.id));
    const scene = [...state.scenes].reverse().find((s) => !busy.has(s.id));

    const from = l2.out.length;
    await inject({
      type: 'SampleLoad',
      payload: {
        track: { id: track.id }, scene: { id: scene.id },
        target: { kind: 'slot' },
        sample: { path: 'Samples/якого-немає.wav', name: 'якого-немає.wav' },
      },
    });
    await waitFor(l2, /SampleLoad ВІДХИЛЕНО \(семпла Samples\/якого-немає\.wav ще немає/, 6000, from);
  });

  await check('семпл на паді Drum Rack доїжджає, а не перетворюється на голий Simpler', async () => {
    // У живому Live семпл на паді народжує НОВИЙ ланцюг усередині рака,
    // а дифф девайсів нові контейнери навмисно пропускає. Перевірено на
    // 12.3.5: без окремого шляху там повна тиша, і партнер не дістає нічого.
    const made = l2.out.length;
    l1.stdin.write('adddevice 0 Drum Rack\n');
    await waitFor(l2, /<- #\d+ DeviceInsert .*Drum Rack/, 8000, made);

    const from = l2.out.length;
    l1.stdin.write('droppad 0 38 Samples/kick.wav\n');
    await waitFor(l1, /поклав Samples\/kick\.wav на пад 38/, 8000);
    await waitFor(l2, /<- #\d+ SampleLoad .*"kind":"drum_pad".*"note":38/, 8000, from);
    const seen = l2.out.slice(from);
    if (/SampleLoad ВІДХИЛЕНО/.test(seen)) throw new Error('партнер відхилив семпл на пад');

    const shown = l2.out.length;
    l2.stdin.write('state\n');
    await waitFor(l2, /"tempo"/, 8000, shown);
    if (!/"38": "Samples\/kick\.wav"/.test(l2.out.slice(shown))) {
      throw new Error('на паді 38 у партнера семпла немає');
    }
  });

  await check('семпл у лінійці їде як семпл, а не як порожня структура', async () => {
    // Audio-кліп в Arrangement партнер не створить із нічого -- зате
    // завантажить той самий файл. Тому структурної події тут немає взагалі:
    // ArrangementClipCreate із is_midi:false приймальний бік чесно відхилив би.
    const dropped = l2.out.length;
    l1.stdin.write('dropsample 2 6 Samples/kick.wav\n');
    await waitFor(l2, /<- #\d+ SampleLoad .*"kind":"slot"/, 8000, dropped);

    const from = l2.out.length;
    l1.stdin.write('arr 2 6 32\n');
    await waitFor(l2, /<- #\d+ SampleLoad .*"kind":"arrangement".*"start_time":32/, 8000, from);
    const seen = l2.out.slice(from);
    if (/ArrangementClipCreate/.test(seen)) {
      throw new Error('audio-кліп полетів структурною подією, яку партнер відхилить');
    }
    if (/SampleLoad ВІДХИЛЕНО/.test(seen)) throw new Error('партнер відхилив семпл у лінійці');

    const shown = l2.out.length;
    l2.stdin.write('state\n');
    await waitFor(l2, /"tempo"/, 8000, shown);
    const state = l2.out.slice(shown);
    if (!/"start_time": 32/.test(state)) throw new Error('кліпа на 32-й долі в партнера немає');
  });

  await check('розмір такту й тональність доїжджають, сміття -- ні', async () => {
    // Без цього партнери в різному метрі: ті самі позиції нот і меж кліпів
    // означають у них різне, і ClipLaunch спрацьовує в різний момент.
    const from = l2.out.length;
    l1.stdin.write('songprop signature_numerator 6\n');
    await waitFor(l2, /<- #\d+ SongPropSet .*"signature_numerator".*"value":6/, 8000, from);
    l1.stdin.write('songprop clip_trigger_quantization 2\n');
    await waitFor(l2, /<- #\d+ SongPropSet .*"clip_trigger_quantization".*"value":2/, 8000, from);
    l1.stdin.write('songprop root_note 5\n');
    await waitFor(l2, /<- #\d+ SongPropSet .*"root_note".*"value":5/, 8000, from);
    if (/SongPropSet ВІДХИЛЕНО/.test(l2.out.slice(from))) throw new Error('партнер відхилив властивість');

    const shown = l2.out.length;
    l2.stdin.write('state\n');
    await waitFor(l2, /"tempo"/, 8000, shown);
    const state = l2.out.slice(shown);
    if (!/"signature_numerator": 6/.test(state)) throw new Error('метр у партнера не змінився');
    if (!/"root_note": 5/.test(state)) throw new Error('тональність у партнера не змінилась');

    // Знаменник розміру Live приймає лише степенем двійки: п'ятірку він
    // мовчки округлив би, і партнери розійшлись би, не помітивши
    const bad = l2.out.length;
    await inject({ type: 'SongPropSet', payload: { prop: 'signature_denominator', value: 5 } });
    await waitFor(l2, /SongPropSet ВІДХИЛЕНО \(некоректне значення 5/, 6000, bad);

    const unknown = l2.out.length;
    await inject({ type: 'SongPropSet', payload: { prop: 'metronome', value: true } });
    await waitFor(l2, /SongPropSet ВІДХИЛЕНО \(невідома властивість пісні metronome\)/, 6000, unknown);
  });

  await check('темп і метр сцени доїжджають одним блоком', async () => {
    // Live віддає -1 замість значення, доки перевизначення вимкнене, тож
    // пʼять полів взаємозалежні: окремими подіями вони пройшли б через стан,
    // у якому значення просто нікуди писати.
    const from = l2.out.length;
    l1.stdin.write('scenetiming 2 140 6 8\n');
    await waitFor(l2, /<- #\d+ SceneTimingSet .*"tempo":140.*"time_signature_numerator":6/, 8000, from);
    if (/SceneTimingSet ВІДХИЛЕНО/.test(l2.out.slice(from))) throw new Error('партнер відхилив блок');

    const shown = l2.out.length;
    l2.stdin.write('state\n');
    await waitFor(l2, /"tempo"/, 8000, shown);
    const state = l2.out.slice(shown);
    if (!/"tempo": 140/.test(state)) throw new Error('темп сцени в партнера не змінився');
    if (!/"time_signature_denominator": 8/.test(state)) throw new Error('метр сцени не доїхав');

    // Знаменник поза степенями двійки Live округлив би мовчки
    const bad = l2.out.length;
    const stateFile = JSON.parse(readFileSync(join(tmp, 'p1.e2e.state.json'), 'utf8'));
    await inject({
      type: 'SceneTimingSet',
      payload: {
        scene: { id: stateFile.scenes[0].id },
        tempo_enabled: false, time_signature_enabled: true,
        time_signature_numerator: 6, time_signature_denominator: 5,
      },
    });
    await waitFor(l2, /SceneTimingSet ВІДХИЛЕНО \(некоректний блок/, 6000, bad);
  });

  await check('властивості audio-кліпа доїжджають, сміття -- ні', async () => {
    // Для аудіо це половина звучання: gain, warp, pitch. Досі ми не возили
    // з них нічого, тож семпл у партнера грав інакше, ніж в автора.
    const born = l2.out.length;
    l1.stdin.write('dropsample 3 1 Samples/kick.wav\n');
    await waitFor(l2, /<- #\d+ SampleLoad .*"kind":"slot"/, 8000, born);

    const from = l2.out.length;
    l1.stdin.write('clipprop 3 1 gain 0.42\n');
    await waitFor(l2, /<- #\d+ ClipPropSet .*"gain".*"value":0\.42/, 8000, from);
    l1.stdin.write('clipprop 3 1 warping true\n');
    await waitFor(l2, /<- #\d+ ClipPropSet .*"warping".*"value":true/, 8000, from);
    l1.stdin.write('clipprop 3 1 pitch_coarse -5\n');
    await waitFor(l2, /<- #\d+ ClipPropSet .*"pitch_coarse".*"value":-5/, 8000, from);
    if (/ClipPropSet ВІДХИЛЕНО/.test(l2.out.slice(from))) throw new Error('партнер відхилив властивість');

    const shown = l2.out.length;
    l2.stdin.write('state\n');
    await waitFor(l2, /"tempo"/, 8000, shown);
    const state = l2.out.slice(shown);
    if (!/"gain": 0\.42/.test(state)) throw new Error('gain у партнера не змінився');
    if (!/"pitch_coarse": -5/.test(state)) throw new Error('транспонування не доїхало');

    const bad = l2.out.length;
    await inject({
      type: 'ClipPropSet',
      payload: { track: { id: 'deadbeefcafe' }, scene: { id: 's' }, prop: 'gain', value: 99 },
    });
    await waitFor(l2, /ClipPropSet ВІДХИЛЕНО \(невідомий трек\)/, 6000, bad);
  });

  await check('петля Arrangement доїжджає, режими запису -- ні', async () => {
    // Петля -- це «де ми зараз працюємо», найспільніше, що взагалі є.
    const from = l2.out.length;
    l1.stdin.write('songprop loop true\n');
    await waitFor(l2, /<- #\d+ SongPropSet .*"loop".*"value":true/, 8000, from);
    l1.stdin.write('songprop loop_start 32\n');
    await waitFor(l2, /<- #\d+ SongPropSet .*"loop_start".*"value":32/, 8000, from);
    l1.stdin.write('songprop loop_length 8\n');
    await waitFor(l2, /<- #\d+ SongPropSet .*"loop_length".*"value":8/, 8000, from);

    const shown = l2.out.length;
    l2.stdin.write('state\n');
    await waitFor(l2, /"tempo"/, 8000, shown);
    const state = l2.out.slice(shown);
    if (!/"loop_start": 32/.test(state)) throw new Error('початок петлі не доїхав');

    // Нульова довжина -- не петля: Live підтягнув би її мовчки
    const bad = l2.out.length;
    await inject({ type: 'SongPropSet', payload: { prop: 'loop_length', value: 0 } });
    await waitFor(l2, /SongPropSet ВІДХИЛЕНО \(некоректне значення 0/, 6000, bad);

    // Режим запису -- намір людини, а не стан документа: приїхавши, він
    // почав би писати озброєні треки партнера його ж входом
    const rec = l2.out.length;
    await inject({ type: 'SongPropSet', payload: { prop: 'record_mode', value: true } });
    await waitFor(l2, /SongPropSet ВІДХИЛЕНО \(невідома властивість пісні record_mode\)/, 6000, rec);
  });

  await check('локатори доїжджають і зникають', async () => {
    // Партнер без них бачить голу лінійку: «Verse», «Drop» -- це структура
    // документа, а не чиясь особиста позначка.
    const from = l2.out.length;
    l1.stdin.write('cue 32 Drop\n');
    await waitFor(l2, /<- #\d+ CueSet .*"time":32.*"name":"Drop"/, 8000, from);
    l1.stdin.write('cue 64 Outro\n');
    await waitFor(l2, /<- #\d+ CueSet .*"time":64/, 8000, from);
    if (/CueSet ВІДХИЛЕНО/.test(l2.out.slice(from))) throw new Error('партнер відхилив локатор');

    const shown = l2.out.length;
    l2.stdin.write('state\n');
    await waitFor(l2, /"tempo"/, 8000, shown);
    if (!/"name": "Drop"/.test(l2.out.slice(shown))) throw new Error('локатора в партнера немає');

    const gone = l2.out.length;
    l1.stdin.write('delcue 32\n');
    await waitFor(l2, /<- #\d+ CueDelete .*"time":32/, 8000, gone);

    // Повторне видалення -- tombstone: локатора вже немає, і це не помилка
    const twice = l2.out.length;
    await inject({ type: 'CueDelete', payload: { time: 32 } });
    await waitFor(l2, /<- #\d+ CueDelete/, 6000, twice);
    if (/CueDelete ВІДХИЛЕНО/.test(l2.out.slice(twice))) {
      throw new Error('повторне видалення не має бути помилкою');
    }
  });

  await check('сенд у чужий Return відхиляється, а не їде не туди', async () => {
    // Індекс сенда -- позиція, а не адреса. Щойно набір Return-треків
    // розійшовся, той самий індекс означає інший ревер -- і мікс тихо
    // псується. Контрольна сума робить це гучним.
    const state = JSON.parse(readFileSync(join(tmp, 'p1.e2e.state.json'), 'utf8'));
    const track = state.tracks.find((t) => t.id);

    const good = l2.out.length;
    l1.stdin.write('send 0 0 0.33\n');
    await waitFor(l2, /<- #\d+ MixerSet .*"param":"send".*0\.33/, 8000, good);
    if (/MixerSet ВІДХИЛЕНО/.test(l2.out.slice(good))) {
      throw new Error('нормальний сенд відхилено');
    }
    if (!/"return"/.test(l2.out.slice(good))) {
      throw new Error('подія поїхала без контрольної суми Return');
    }

    const bad = l2.out.length;
    await inject({
      type: 'MixerSet',
      payload: {
        track: { id: track.id }, param: 'send', index: 0, value: 0.9,
        return: { id: 'ffffffffffff' },
      },
    });
    await waitFor(l2, /MixerSet ВІДХИЛЕНО \(сенд 0 веде в різні Return-треки/, 6000, bad);
  });

  await check('Return-трек доїжджає, і сенди після нього лишаються на місці', async () => {
    // Набір Return-треків визначає, ЩО означає index сенда. Доки він
    // не синхронізувався, той самий index на двох машинах вів у різні ревери.
    const from = l2.out.length;
    l1.stdin.write('addreturn C-Tape\n');
    await waitFor(l2, /<- #\d+ ReturnCreate .*C-Tape/, 8000, from);
    if (/ReturnCreate ВІДХИЛЕНО/.test(l2.out.slice(from))) {
      throw new Error('партнер відхилив Return');
    }

    // Тепер сенд у новий Return має пройти контрольну суму з обох боків
    const sent = l2.out.length;
    l1.stdin.write('send 0 2 0.55\n');
    await waitFor(l2, /<- #\d+ MixerSet .*"value":0\.55.*"index":2/, 8000, sent);
    if (/MixerSet ВІДХИЛЕНО/.test(l2.out.slice(sent))) {
      throw new Error('сенд у щойно створений Return відхилено — набори розійшлись');
    }

    const gone = l2.out.length;
    l1.stdin.write('delreturn 2\n');
    await waitFor(l2, /<- #\d+ ReturnDelete/, 8000, gone);
  });

  await check('призначення кросфейдера доїжджає, сміття -- ні', async () => {
    // Не DeviceParameter, а звичайна int-властивість mixer_device: у загальний
    // цикл параметрів вона не потрапляє, тож потребує власного шляху.
    const from = l2.out.length;
    l1.stdin.write('xfade 0 2\n');
    await waitFor(l2, /<- #\d+ MixerSet .*"crossfade_assign".*"value":2/, 8000, from);
    if (/MixerSet ВІДХИЛЕНО/.test(l2.out.slice(from))) throw new Error('партнер відхилив');

    const shown = l2.out.length;
    l2.stdin.write('state\n');
    await waitFor(l2, /"tempo"/, 8000, shown);
    if (!/"crossfade_assign:-": 2/.test(l2.out.slice(shown))) {
      throw new Error('призначення в партнера не змінилось');
    }

    const bad = l2.out.length;
    const state = JSON.parse(readFileSync(join(tmp, 'p1.e2e.state.json'), 'utf8'));
    await inject({
      type: 'MixerSet',
      payload: { track: { id: state.tracks[0].id }, param: 'crossfade_assign', value: 7 },
    });
    await waitFor(l2, /MixerSet ВІДХИЛЕНО \(crossfade_assign 7 поза межами\)/, 6000, bad);
  });

  await check('warp-маркери доїжджають повним набором', async () => {
    // Маркери описують ВІДОБРАЖЕННЯ файлу на долі. Частковий набір -- не
    // «майже правильно», а інший ритм, тож вони їдуть разом.
    const born = l2.out.length;
    l1.stdin.write('dropsample 3 4 Samples/kick.wav\n');
    await waitFor(l2, /<- #\d+ SampleLoad .*"kind":"slot"/, 8000, born);

    const from = l2.out.length;
    l1.stdin.write('warp 3 4 0:0 4:1.5 8:3.25\n');
    await waitFor(l2, /<- #\d+ ClipWarpSet .*"beat_time":8/, 8000, from);
    if (/ClipWarpSet ВІДХИЛЕНО/.test(l2.out.slice(from))) {
      throw new Error('партнер відхилив маркери');
    }

    const shown = l2.out.length;
    l2.stdin.write('state\n');
    await waitFor(l2, /"tempo"/, 8000, shown);
    if (!/"sample_time": 3\.25/.test(l2.out.slice(shown))) {
      throw new Error('маркери в партнера не лягли');
    }

    // На MIDI-кліпі warp-маркерів немає -- це різниця типів, не помилка
    const midi = l2.out.length;
    const state = JSON.parse(readFileSync(join(tmp, 'p1.e2e.state.json'), 'utf8'));
    const withClip = state.tracks.find((t) => (t.clips || []).some((c) => c.notes));
    const scene = withClip.clips.find((c) => c.notes).scene.id;
    await inject({
      type: 'ClipWarpSet',
      payload: { track: { id: withClip.id }, scene: { id: scene },
                 markers: [{ beat_time: 0, sample_time: 0 }] },
    });
    await waitFor(l2, /ClipWarpSet ВІДХИЛЕНО \(warp-маркерів у MIDI-кліпа немає\)/, 6000, midi);
  });

  await check('властивості кліпа в лінійці адресуються uuid, не сценою', async () => {
    // У лінійці сцен немає, зате є uuid. Одна подія має вміти в обидва
    // способи -- інакше аранжування лишалось би без gain, warp і меж.
    const dropped = l2.out.length;
    l1.stdin.write('dropsample 3 7 Samples/kick.wav\n');
    await waitFor(l2, /<- #\d+ SampleLoad .*"kind":"slot"/, 8000, dropped);

    const born = l2.out.length;
    l1.stdin.write('arr 3 7 40\n');
    await waitFor(l2, /<- #\d+ SampleLoad .*"kind":"arrangement"/, 8000, born);

    const state = JSON.parse(readFileSync(join(tmp, 'p1.e2e.state.json'), 'utf8'));
    const from = d1.out.length;
    l1.stdin.write('fullstate\n');
    await waitFor(d1, /state: знімок \d+ зібрано/, 10000, from);
    const mine = JSON.parse(readFileSync(join(tmp, 'p1.e2e.state.json'), 'utf8'));
    const arrClip = mine.tracks.flatMap((t) => t.arrangement || []).find((c) => c.id);
    if (!arrClip) throw new Error('кліпа в лінійці немає у знімку');

    const sent = l2.out.length;
    await inject({
      type: 'ClipPropSet',
      payload: { track: { id: mine.tracks.find((t) => (t.arrangement || []).length).id },
                 clip: { id: arrClip.id }, prop: 'gain', value: 0.31 },
    });
    await waitFor(l2, /<- #\d+ ClipPropSet .*"gain".*0\.31/, 8000, sent);
    if (/ClipPropSet ВІДХИЛЕНО/.test(l2.out.slice(sent))) {
      throw new Error('партнер не знайшов кліп у лінійці за uuid');
    }
  });

  await check('журнал: 120 подій, монотонний gseq, цілий hash-chain', async () => {
    await new Promise((r) => setTimeout(r, 400));
    const lines = readFileSync(join(tmp, `${SESSION}.jsonl`), 'utf8').split('\n').filter(Boolean);
    if (lines.length !== 120) throw new Error(`очікував 120 подій, у журналі ${lines.length}`);
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

  await check('verify.js підтверджує цілісність сесії і ловить підміну', async () => {
    const run = (args) => new Promise((resolve) => {
      const p = spawn(process.execPath, [join(root, 'relay/verify.js'), ...args], { cwd: root });
      let out = '';
      p.stdout.on('data', (b) => { out += b; });
      p.stderr.on('data', (b) => { out += b; });
      p.on('close', (code) => resolve({ code, out }));
    });

    const ok = await run([SESSION, '--dir', tmp]);
    if (ok.code !== 0) throw new Error(`чистий журнал не пройшов: ${ok.out}`);
    if (!/цілісність підтверджена/.test(ok.out)) throw new Error(ok.out);

    // Підміняємо значення в середині журналу, лишаючи hash недоторканим --
    // саме так виглядав би тихо відредагований файл
    const path = join(tmp, `${SESSION}.jsonl`);
    const original = readFileSync(path, 'utf8');
    const lines = original.split('\n').filter(Boolean);
    const at = Math.floor(lines.length / 2);
    const forged = JSON.parse(lines[at]);
    forged.author = `${forged.author}-підробка`;
    lines[at] = JSON.stringify(forged);
    writeFileSync(path, lines.join('\n') + '\n');
    try {
      const bad = await run([SESSION, '--dir', tmp]);
      if (bad.code === 0) throw new Error('підміну не помічено');
      if (!/ПРОБЛЕМА/.test(bad.out)) throw new Error(`мовчазна відмова: ${bad.out}`);
    } finally {
      writeFileSync(path, original);
    }
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

  let lLater = null;
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

    lLater = mkLive(5, 19953, 19954);
    await waitFor(dLater, /застосовую \d+ відкладених подій/, 15000);
    await waitFor(lLater, /<- #\d+ TempoSet/, 10000);
  });

  await check('стиснутий хвіст доводить пізнього гравця до того ж стану', async () => {
    // p4 приєднався з since=0, тож relay віддав йому весь журнал -- але вже
    // без подій, які перекриті пізнішими (relay/compact.js)
    await waitFor(relay, /\+ p4 .*хвіст \d+ -> \d+/, 5000);

    const stateFrom = lLater.out.length;
    lLater.stdin.write('state\n');
    await waitFor(lLater, /"tempo": 128/, 10000, stateFrom);
    if (!/"name": "Bass Lead"/.test(lLater.out.slice(stateFrom))) {
      throw new Error('назва треку не доїхала стиснутим хвостом');
    }
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
