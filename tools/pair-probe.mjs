// Прогін «живий Live + емулятор» -- на одній машині.
//
// Половина того, що ми пишемо, вимагає ДВОХ учасників: присутність, follow,
// undo, обмін знімками, узгодження можливостей. Другим учасником не мусить
// бути Live -- fake-live говорить тим самим протоколом. Це не заміна прогону
// парою (емулятор не має справжніх LOM-listener-ів), але воно перевіряє весь
// протокольний бік із реальним bridge на одному кінці.
//
// Передумова: relay і daemon p1 на живому Live уже підняті.
//
//   node tools/pair-probe.mjs [--relay ws://127.0.0.1:19870] [--session pair]

import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { homedir, tmpdir } from 'node:os';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const arg = (name, fallback) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 ? process.argv[i + 1] : fallback;
};
const RELAY = arg('relay', 'ws://127.0.0.1:19870');
const SESSION = arg('session', 'pair');
const STATE = arg('state-dir', join(tmpdir(), 'abletonmp-pair'));
const PROJECT = join(STATE, 'project-p2');

/** Дія на боці p1 -- через HTTP API самого bridge. Інакше інструмент
 *  залежав би від того, що людина щось устигла натиснути. */
async function p1exec(actions) {
  const token = readFileSync(join(homedir(), '.abletonmp', 'chat_token'), 'utf8').trim();
  const res = await fetch('http://127.0.0.1:19847/api/exec', {
    method: 'POST',
    headers: { 'X-AbletonMP-Token': token, 'Content-Type': 'application/json' },
    body: JSON.stringify({ actions }),
  });
  return res.json();
}

const procs = [];
let failed = 0;

function launch(name, file, args) {
  const p = spawn(process.execPath, [file, ...args], { cwd: join(root, 'daemon') });
  p.out = '';
  const collect = (b) => { p.out += b.toString(); if (process.env.PAIR_VERBOSE) process.stdout.write(`[${name}] ${b}`); };
  p.stdout.on('data', collect);
  p.stderr.on('data', collect);
  p.name = name;
  procs.push(p);
  return p;
}

function waitFor(p, pattern, ms = 15000, from = 0) {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + ms;
    const tick = setInterval(() => {
      if (pattern.test(p.out.slice(from))) { clearInterval(tick); resolve(); }
      else if (Date.now() > deadline) {
        clearInterval(tick);
        reject(new Error(`[${p.name}] не дочекався ${pattern}`));
      }
    }, 50);
  });
}

async function check(label, fn) {
  try { await fn(); console.log(`  ok   ${label}`); }
  catch (e) { failed += 1; console.log(`  FAIL ${label}\n       ${e.message.split('\n')[0]}`); }
}

// ------------------------------------------------------------------ сценарій

mkdirSync(join(PROJECT, 'Samples'), { recursive: true });

const d2 = launch('daemon-p2', join(root, 'daemon/index.js'), [
  '--author', 'p2', '--session', SESSION, '--relay', RELAY,
  '--udp-in', '19855', '--udp-out', '19856',
  '--state-dir', STATE, '--project', PROJECT,
]);
const l2 = launch('live-p2', join(root, 'daemon/tools/fake-live.js'), [
  '--udp-in', '19855', '--udp-out', '19856', '--project', PROJECT,
]);

try {
  console.log(`сесія ${SESSION}, relay ${RELAY}\n`);
  await waitFor(d2, /relay: head=/);
  await waitFor(d2, /bridge підключився/);

  await check('обидва учасники бачать один одного', async () => {
    await waitFor(d2, /у сесії:.*p1/, 20000);
  });

  await check('розбіжність можливостей називається поіменно', async () => {
    // Це головна цінність двох учасників: relay звіряє переліки типів
    // і каже, ЩО саме в партнера не спрацює -- замість «щось не так».
    await waitFor(d2, /НЕСУМІСНІСТЬ: p1 не вміє застосовувати: /, 20000);
    const line = (d2.out.match(/p1 не вміє застосовувати: ([^\n]+)/) || [])[1] || '';
    console.log(`       p1 не вміє: ${line.split(' —')[0]}`);
  });

  await check('присутність: p2 бачить, куди дивиться p1', async () => {
    const from = d2.out.length;
    d2.stdin.write('who\n');
    await waitFor(d2, /дивляться: p1|ніхто нікуди не дивиться/, 8000, from);
    if (/ніхто нікуди не дивиться/.test(d2.out.slice(from))) {
      throw new Error('p1 не повідомив свій вид');
    }
  });

  await check('знімок p1 доїжджає до p2 через relay', async () => {
    const from = d2.out.length;
    d2.stdin.write('pull p1\n');
    await waitFor(d2, /знімок застосовано: \d+ з \d+|знімок від p1/, 30000, from);
  });

  await check('follow веде вид p2 за p1 і сам вимикається', async () => {
    const from = d2.out.length;
    d2.stdin.write('follow p1\n');
    await waitFor(d2, /слідую за p1/, 8000, from);
    // Перевіряємо не лог наміру, а факт: вид p2 має реально переїхати.
    // Рухаємо двічі: перший виклик міг збігтися з тим, де p1 уже стоїть,
    // а неруханий вид події присутності не породжує.
    const moved = l2.out.length;
    for (const idx of [0, 2]) {
      await p1exec([{ op: 'lom_set', path: ['song', 'view'], property: 'selected_track',
                      value: { $path: ['song', 'tracks', idx] } }]);
      await new Promise((r) => setTimeout(r, 900));
    }
    await waitFor(l2, /<- view_set від p1:/, 12000, moved);
    await new Promise((r) => setTimeout(r, 1200));
    d2.stdin.write('follow off\n');
    await waitFor(d2, /більше не слідую/, 8000, from);
  });

  await check('файл із теки проєкту доїжджає до партнера', async () => {
    const blob = Buffer.alloc(64 * 1024);
    for (let i = 0; i < blob.length; i += 1) blob[i] = (i * 31 + 7) & 0xff;
    const name = `probe-${Date.now() % 100000}.wav`;
    writeFileSync(join(PROJECT, 'Samples', name), blob);

    // Перевіряємо не власний перескан, а те, що файл ДОЇХАВ: беремо теку
    // проєкту p1 з його ж знімка й дивимось, чи з'явився там файл.
    const snap = await p1exec([{ op: 'snapshot' }]);
    const alsPath = snap?.result?.snapshot?.file_path;
    if (!alsPath) return console.log('       p1 без збереженого сету — перевірку пропущено');
    const target = join(dirname(alsPath), 'Samples', name);
    const deadline = Date.now() + 30000;   // перескан filesync раз на 10 с
    while (Date.now() < deadline) {
      if (existsSync(target)) return;
      await new Promise((r) => setTimeout(r, 500));
    }
    throw new Error(`файл не доїхав у ${target}`);
  });

  await check('status відповідає на «у нас усе гаразд?» одним екраном', async () => {
    const from = d2.out.length;
    d2.stdin.write('status\n');
    await waitFor(d2, /семпли: \d+ файлів/, 8000, from);
    const said = d2.out.slice(from);
    for (const [what, re] of [
      ['сесію', /сесія /],
      ['звʼязок із relay', /relay: підключено|relay: НЕМАЄ/],
      ['стан bridge', /bridge: Live |bridge: не на звʼязку/],
      ['партнерів', /партнери: /],
    ]) {
      if (!re.test(said)) throw new Error(`status не показав ${what}`);
    }
    if (!/партнери: p1/.test(said)) throw new Error('status не побачив p1');
  });

  await check('лок видно партнеру: «редагують» називає, хто саме', async () => {
    // Неперервний жест бере лок на обʼєкт. Це не блокування -- це підказка
    // партнеру, щоб двоє не крутили той самий фейдер наосліп.
    const from = d2.out.length;
    for (let i = 0; i < 4; i += 1) {
      await p1exec([{ op: 'set_mixer', track_index: 0, param: 'volume', value: 0.6 + i * 0.05 }]);
      await new Promise((r) => setTimeout(r, 250));
    }
    await waitFor(d2, /редагують: p1/, 12000, from);
    // Лок відпускається сам після паузи -- без цього чужий фейдер лишався б
    // «зайнятим» назавжди після кожного дотику
    await waitFor(d2, /ніхто нічого не редагує/, 20000, from);
  });

  await check('diff називає розбіжність, не змінюючи стану', async () => {
    // Діагностика не має чіпати те, що діагностує: pull застосовує чужий
    // знімок, diff лише порівнює.
    const from = d2.out.length;
    d2.stdin.write('diff p1\n');
    await waitFor(d2, /розбіжності з p1 \(\d+\)|стан збігається з p1/, 30000, from);
    const said = d2.out.slice(from);
    if (/знімок застосовано/.test(said)) {
      throw new Error('diff застосував знімок замість порівняння');
    }
    const shown = (said.match(/  · [^\n]+/g) || []).slice(0, 3);
    for (const line of shown) console.log(`     ${line.trim()}`);
  });

  await check('Arrangement: MIDI-кліп доїжджає, переїжджає і зникає', async () => {
    // Найтонше місце: у партнера кліп збирається в тимчасовому слоті Session
    // і вже звідти дублюється в лінійку. Тимчасовий кліп не має протекти.
    const from = l2.out.length;
    await p1exec([{ op: 'create_midi_clip', track_index: 1, scene_index: 0, length: 4 }]);
    await new Promise((r) => setTimeout(r, 700));
    await p1exec([{ op: 'lom_call', path: ['song', 'tracks', 1], method: 'duplicate_clip_to_arrangement',
                    args: [{ $path: ['song', 'tracks', 1, 'clip_slots', 0, 'clip'] }, 96.0] }]);
    await waitFor(l2, /<- #\d+ ArrangementClipCreate .*"start_time":96/, 15000, from);
    if (/ArrangementClipCreate ВІДХИЛЕНО/.test(l2.out.slice(from))) {
      throw new Error('партнер відхилив кліп у лінійці');
    }

    // Переїзд робиться як копія плюс видалення старої -- один DeviceMove
    // тут не існує, зате uuid має лишитись тим самим
    const moved = l2.out.length;
    await p1exec([{ op: 'lom_call', path: ['song', 'tracks', 1], method: 'duplicate_clip_to_arrangement',
                    args: [{ $path: ['song', 'tracks', 1, 'arrangement_clips', 0] }, 128.0] },
                  { op: 'lom_call', path: ['song', 'tracks', 1], method: 'delete_clip',
                    args: [{ $path: ['song', 'tracks', 1, 'arrangement_clips', 0] }] }]);
    await waitFor(l2, /<- #\d+ ArrangementClip(Move|Create)/, 15000, moved);

    // Прибираємо за собою: і в лінійці, і в слоті
    await p1exec([{ op: 'lom_call', path: ['song', 'tracks', 1], method: 'delete_clip',
                    args: [{ $path: ['song', 'tracks', 1, 'arrangement_clips', 0] }] },
                  { op: 'lom_call', path: ['song', 'tracks', 1, 'clip_slots', 0], method: 'delete_clip' }]);
    await waitFor(l2, /<- #\d+ ArrangementClipDelete/, 15000, moved);
  });

  await check('пізній учасник дістає стиснутий хвіст, а не всю історію', async () => {
    // Серія змін темпу -- шість подій за однією адресою. Той, хто приєднався
    // пізніше, має отримати одну: стан той самий, але Live не проганяє
    // пʼять проміжних положень фейдера.
    for (const bpm of [110, 112, 114, 116, 118, 120]) {
      await p1exec([{ op: 'set_tempo', bpm }]);
      await new Promise((r) => setTimeout(r, 220));
    }
    await new Promise((r) => setTimeout(r, 800));

    const lateState = join(STATE, 'late');
    mkdirSync(join(lateState, 'Samples'), { recursive: true });
    const d3 = launch('daemon-p3', join(root, 'daemon/index.js'), [
      '--author', 'p3', '--session', SESSION, '--relay', RELAY,
      '--udp-in', '19865', '--udp-out', '19866',
      '--state-dir', lateState, '--project', lateState,
    ]);
    const l3 = launch('live-p3', join(root, 'daemon/tools/fake-live.js'), [
      '--udp-in', '19865', '--udp-out', '19866', '--project', lateState,
    ]);
    try {
      await waitFor(d3, /relay: head=/, 20000);
      await waitFor(l3, /<- #\d+ TempoSet/, 20000);
      await new Promise((r) => setTimeout(r, 1500));
      const got = (l3.out.match(/<- #\d+ TempoSet/g) || []).length;
      if (got > 2) throw new Error(`хвіст не стиснувся: ${got} подій TempoSet замість однієї`);
      console.log(`       пізній учасник отримав ${got} TempoSet із шести`);
    } finally {
      d3.kill();
      l3.kill();
    }
  });

  await check('undo: p2 відкочує зміну p1', async () => {
    // Відкотити можна лише туди, де в журналі Є попереднє значення за тією
    // ж адресою. Одна зміна темпу цього не дає: попереднє лежить у .als,
    // а не в сесії. Тому робимо дві.
    await p1exec([{ op: 'set_tempo', bpm: 121 }]);
    await new Promise((r) => setTimeout(r, 700));
    await p1exec([{ op: 'set_tempo', bpm: 133 }]);
    await new Promise((r) => setTimeout(r, 700));

    const from = d2.out.length;
    d2.stdin.write('undo p1\n');
    await waitFor(d2, /відкочую \w+ від p1/, 15000, from);
  });
} catch (e) {
  failed += 1;
  console.log(`\nсценарій обірвався: ${e.message}`);
} finally {
  for (const p of procs) { try { p.kill(); } catch {} }
}

console.log(failed ? `\n${failed} перевірок впало` : '\nусі перевірки пройшли');
process.exit(failed ? 1 : 0);
