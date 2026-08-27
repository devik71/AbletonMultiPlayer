// Прогін на ДВОХ живих машинах, керований з однієї.
//
// pair-probe ставить емулятор другим учасником -- це перевіряє протокол, але
// не перевіряє Live. Тут обидва кінці справжні: p1 -- локальний, p2 -- через
// SSH. Різні ОС і різні версії Live тут не завада, а сенс: саме на такій парі
// вилізло, що macOS не пускає датаграму понад 9216 байтів, а Ableton
// перейменовує параметри девайсів між мінорними версіями.
//
// Передумови: relay і daemon підняті з обох боків, SSH працює за ключем.
//
//   node tools/pair-live.mjs --peer macbook@192.168.3.18 \
//        --peer-log '~/abletonmp-run/p2.log' --peer-cmd '~/abletonmp-run/p2.cmd'

import { execFileSync } from 'node:child_process';
import { appendFileSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

const arg = (name, fallback) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 ? process.argv[i + 1] : fallback;
};

const PEER = arg('peer', null);
const PEER_LOG = arg('peer-log', '~/abletonmp-run/p2.log');
const PEER_CMD = arg('peer-cmd', '~/abletonmp-run/p2.cmd');
const MY_LOG = arg('my-log', null);
const MY_CMD = arg('my-cmd', null);

if (!PEER) {
  console.error('потрібен --peer user@host');
  process.exit(2);
}

const ssh = (script) => execFileSync('ssh',
  ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', PEER, script],
  { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });

/** Дія в Live через локальний HTTP-API bridge. */
async function exec1(actions) {
  const token = readFileSync(join(homedir(), '.abletonmp', 'chat_token'), 'utf8').trim();
  const res = await fetch('http://127.0.0.1:19847/api/exec', {
    method: 'POST',
    headers: { 'X-AbletonMP-Token': token, 'Content-Type': 'application/json' },
    body: JSON.stringify({ actions }),
  });
  return res.json();
}

/** Те саме, але в Live партнера -- curl запускається на його машині. */
function exec2(actions) {
  const body = JSON.stringify({ actions }).replace(/'/g, `'\\''`);
  const out = ssh(`T=$(cat ~/.abletonmp/chat_token); curl -s --max-time 15 `
    + `-X POST http://127.0.0.1:19847/api/exec `
    + `-H "X-AbletonMP-Token: $T" -H "Content-Type: application/json" `
    + `-d '${body}'`);
  try { return JSON.parse(out); } catch { return { ok: false, raw: out }; }
}

const val = (r) => r?.result?.results?.[0]?.result;

const log1 = () => (MY_LOG ? readFileSync(MY_LOG, 'utf8') : '');
const log2 = () => ssh(`cat ${PEER_LOG}`);
const say1 = (line) => { if (MY_CMD) appendFileSync(MY_CMD, `${line}\n`); };
const say2 = (line) => ssh(`printf '%s\\n' ${JSON.stringify(line)} >> ${PEER_CMD}`);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Чекає, доки в лозі не зʼявиться рядок. Лог читається щоразу заново:
 *  на боці партнера це окремий файл на окремій машині. */
async function waitLog(reader, re, ms, from = 0) {
  const deadline = Date.now() + ms;
  let text = '';
  while (Date.now() < deadline) {
    text = reader().slice(from);
    if (re.test(text)) return text;
    await sleep(800);
  }
  throw new Error(`не дочекався ${re}`);
}

let failed = 0;
async function check(name, fn) {
  try {
    await fn();
    console.log(`  ok   ${name}`);
  } catch (e) {
    failed += 1;
    console.log(`  FAIL ${name}`);
    console.log(`       ${e.message}`);
  }
}

// --------------------------------------------------------------------------

console.log(`пара: локальний p1 <-> ${PEER}\n`);

await check('обидві машини на звʼязку, код однаковий', async () => {
  const health = await (await fetch('http://127.0.0.1:19870/health')).json();
  const clients = (health.sessions || []).flatMap((s) => s.clients || []);
  if (clients.length < 2) throw new Error(`у сесії ${clients.length} учасник(ів), треба двоє`);
  const shas = new Set(clients.map((c) => c.sha));
  for (const c of clients) console.log(`       ${c.author}: Live ${c.live}, код ${c.sha}`);
  if (shas.size > 1) throw new Error('машини крутять різний код bridge');
});

await check('темп їде в обидва боки', async () => {
  await exec1([{ op: 'lom_set', path: ['song'], property: 'tempo', value: 128.5 }]);
  await sleep(2500);
  if (val(exec2([{ op: 'lom_get', path: ['song', 'tempo'] }])) !== 128.5) {
    throw new Error('темп не доїхав до партнера');
  }
  exec2([{ op: 'lom_set', path: ['song'], property: 'tempo', value: 119 }]);
  await sleep(2500);
  const back = val(await exec1([{ op: 'lom_get', path: ['song', 'tempo'] }]));
  if (back !== 119) throw new Error(`темп не повернувся: ${back}`);
});

await check('гучність треку їде в обидва боки', async () => {
  await exec1([{ op: 'lom_set', path: ['tracks', 0, 'mixer_device', 'volume'], property: 'value', value: 0.61 }]);
  await sleep(2500);
  const there = val(exec2([{ op: 'lom_get', path: ['tracks', 0, 'mixer_device', 'volume', 'value'] }]));
  if (Math.abs(there - 0.61) > 0.001) throw new Error(`у партнера ${there}`);
  exec2([{ op: 'lom_set', path: ['tracks', 0, 'mixer_device', 'volume'], property: 'value', value: 0.85 }]);
  await sleep(2500);
  const here = val(await exec1([{ op: 'lom_get', path: ['tracks', 0, 'mixer_device', 'volume', 'value'] }]));
  if (Math.abs(here - 0.85) > 0.001) throw new Error(`у мене ${here}`);
});

await check('назва треку їде в обидва боки', async () => {
  const mark = `pair-${Date.now() % 10000}`;
  await exec1([{ op: 'lom_set', path: ['tracks', 1], property: 'name', value: mark }]);
  await sleep(2500);
  if (val(exec2([{ op: 'lom_get', path: ['tracks', 1, 'name'] }])) !== mark) {
    throw new Error('назва не доїхала');
  }
  exec2([{ op: 'lom_set', path: ['tracks', 1], property: 'name', value: '2-MIDI' }]);
  await sleep(2000);
});

await check('присутність: партнер бачить, куди я дивлюсь', async () => {
  const from = log2().length;
  await exec1([{ op: 'lom_set', path: ['song', 'view'], property: 'selected_track',
                 value: { $path: ['tracks', 2] } }]);
  await sleep(2000);
  say2('who');
  await waitLog(log2, /дивляться: p1/, 15000, from);
});

await check('лок: партнер бачить, що я кручу', async () => {
  const from = log2().length;
  for (let i = 0; i < 5; i += 1) {
    await exec1([{ op: 'lom_set', path: ['tracks', 0, 'mixer_device', 'volume'],
                   property: 'value', value: 0.5 + i * 0.03 }]);
    await sleep(250);
  }
  await waitLog(log2, /редагують: p1/, 20000, from);
  await waitLog(log2, /ніхто нічого не редагує/, 30000, from);
});

await check('undo партнера відкочує мою зміну', async () => {
  await exec1([{ op: 'lom_set', path: ['tracks', 0, 'mixer_device', 'volume'], property: 'value', value: 0.2 }]);
  await sleep(1500);
  await exec1([{ op: 'lom_set', path: ['tracks', 0, 'mixer_device', 'volume'], property: 'value', value: 0.9 }]);
  await sleep(2000);
  const from = log2().length;
  say2('undo p1');
  await waitLog(log2, /прошу відкотити|відкот/, 15000, from);
  await sleep(3000);
  const here = val(await exec1([{ op: 'lom_get', path: ['tracks', 0, 'mixer_device', 'volume', 'value'] }]));
  if (Math.abs(here - 0.2) > 0.01) throw new Error(`гучність ${here}, а мала повернутись до 0.2`);
});

await check('кліп із нотами доїжджає до партнера', async () => {
  // Найтонше з базового: кліп, потім ноти окремими подіями, і все це
  // адресується парою (трек, сцена) -- власного uuid у сесійного кліпа немає.
  const scene = 5;
  await exec1([{ op: 'delete_clip', track_index: 1, scene_index: scene }]).catch(() => {});
  await sleep(800);
  await exec1([{ op: 'create_midi_clip', track_index: 1, scene_index: scene, length: 4 }]);
  await sleep(2000);
  await exec1([{ op: 'replace_clip_notes', track_index: 1, scene_index: scene, notes: [
    { pitch: 60, start_time: 0, duration: 0.5, velocity: 100 },
    { pitch: 64, start_time: 1, duration: 0.5, velocity: 90 },
    { pitch: 67, start_time: 2, duration: 0.5, velocity: 80 },
  ] }]);
  await sleep(3000);

  const has = val(exec2([{ op: 'lom_get', path: ['tracks', 1, 'clip_slots', scene, 'has_clip'] }]));
  if (has !== true) throw new Error('кліпа в партнера немає');
  const notes = exec2([{ op: 'lom_call', path: ['tracks', 1, 'clip_slots', scene, 'clip'],
                         method: 'get_notes_extended', args: [0, 128, 0, 8] }]);
  const got = val(notes);
  const count = Array.isArray(got) ? got.length : (got?.notes?.length ?? -1);
  if (count !== 3) throw new Error(`нот у партнера: ${count}, а мало бути 3`);
});

await check('створений трек зʼявляється, видалений зникає', async () => {
  const before = val(exec2([{ op: 'lom_get', path: ['tracks'] }]))?.length;
  await exec1([{ op: 'create_track', kind: 'midi', name: 'PairProbe' }]);
  await sleep(3000);
  const names = val(exec2([{ op: 'lom_get', path: ['tracks'] }])) || [];
  if (!JSON.stringify(names).includes('PairProbe')) {
    throw new Error('трек не доїхав до партнера');
  }
  // Прибираємо за собою -- сет належить людині, а не пробі
  const mine = val(await exec1([{ op: 'lom_get', path: ['tracks'] }])) || [];
  const idx = mine.findIndex((t) => JSON.stringify(t).includes('PairProbe'));
  if (idx >= 0) await exec1([{ op: 'delete_track', track_index: idx }]);
  await sleep(3000);
  const after = val(exec2([{ op: 'lom_get', path: ['tracks'] }]))?.length;
  if (after !== before) throw new Error(`у партнера лишилось ${after} треків замість ${before}`);
});

await check('сцена створюється й зникає з обох боків', async () => {
  const before = val(exec2([{ op: 'lom_get', path: ['scenes'] }]))?.length;
  await exec1([{ op: 'create_scene' }]);
  await sleep(3000);
  const mid = val(exec2([{ op: 'lom_get', path: ['scenes'] }]))?.length;
  if (mid !== before + 1) throw new Error(`сцен у партнера ${mid}, чекали ${before + 1}`);
  const mine = val(await exec1([{ op: 'lom_get', path: ['scenes'] }])) || [];
  await exec1([{ op: 'delete_scene', scene_index: mine.length - 1 }]);
  await sleep(3000);
  const after = val(exec2([{ op: 'lom_get', path: ['scenes'] }]))?.length;
  if (after !== before) throw new Error(`сцен лишилось ${after} замість ${before}`);
});

await check('знімок партнера доїжджає й порівнюється', async () => {
  if (!MY_CMD || !MY_LOG) throw new Error('потрібні --my-cmd і --my-log');
  const from = log1().length;
  say1('refresh');
  await sleep(3000);
  say1('diff p2');
  const text = await waitLog(log1, /розбіжності з p2|стан збігається з p2|предметних розбіжностей/, 60000, from);
  for (const line of (text.match(/  · [^\n]+/g) || []).slice(0, 6)) {
    console.log(`     ${line.trim()}`);
  }
});

console.log(failed ? `\n${failed} перевірок впало` : '\nусі перевірки пройшли');
process.exit(failed ? 1 : 0);
