// Local daemon -- один процес на машину.
//
// Міст між bridge всередині Live (UDP, localhost) і relay (WebSocket).
// Тут живе все, що не можна пускати в процес Live: реконект, буферизація,
// локальний журнал, clock sync.

import { createSocket } from 'node:dgram';
import { readFileSync, writeFileSync, existsSync, mkdirSync, appendFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import WebSocket from 'ws';
import { FileSync } from './filesync.js';

const __dirname = dirname(fileURLToPath(import.meta.url));

// --------------------------------------------------------------------- args

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const AUTHOR = arg('author', 'p1');
const SESSION = arg('session', 'default');
const RELAY = arg('relay', 'ws://127.0.0.1:19870');
const PORT_IN = Number(arg('udp-in', 19845)); // сюди пише bridge
const PORT_OUT = Number(arg('udp-out', 19846)); // сюди слухає bridge
const STATE_DIR = arg('state-dir', join(__dirname, 'state'));

const PROTO = 1;
const RECONNECT_MIN = 500;
const RECONNECT_MAX = 10_000;
const CLOCK_PING_SEC = 15;

mkdirSync(STATE_DIR, { recursive: true });

// Стан ОБОВʼЯЗКОВО прив'язаний до сесії, а не лише до автора: gseq нумерується
// всередині сесії, тож lastGseq зі старої сесії зробив би daemon глухим до нової
// (усі її події виглядали б як «вже бачені»).
const SLUG = `${AUTHOR}.${SESSION}`.replace(/[^\w.-]+/g, '_');
const statePath = join(STATE_DIR, `${SLUG}.json`);
const outboxPath = join(STATE_DIR, `${SLUG}.outbox.jsonl`);
const localJournalPath = join(STATE_DIR, `${SLUG}.applied.jsonl`);
const pendingPath = join(STATE_DIR, `${SLUG}.pending.jsonl`);

function log(...a) {
  // локальний час, щоб збігалося з bridge.log при звірянні логів
  console.log(new Date().toTimeString().slice(0, 8), ...a);
}

// -------------------------------------------------------------------- state

/** lseq персистентний: relay дедуплікує по (author, lseq), тож рестарт daemon
 *  не має права почати нумерацію заново. */
let state = { lseq: 0, lastGseq: 0 };
if (existsSync(statePath)) {
  try {
    state = { ...state, ...JSON.parse(readFileSync(statePath, 'utf8')) };
  } catch (e) {
    log('стан пошкоджений, починаю з нуля:', e.message);
  }
}
const saveState = () => writeFileSync(statePath, JSON.stringify(state));

/** Події, які ще не підтверджені relay. Переживають розрив мережі і рестарт daemon. */
let outbox = [];
if (existsSync(outboxPath)) {
  outbox = readFileSync(outboxPath, 'utf8')
    .split('\n')
    .filter(Boolean)
    .map((l) => {
      try {
        return JSON.parse(l);
      } catch {
        return null;
      }
    })
    .filter(Boolean);
  if (outbox.length) log(`відновлено ${outbox.length} невідправлених подій з outbox`);
}
const saveOutbox = () =>
  writeFileSync(outboxPath, outbox.map((e) => JSON.stringify(e)).join('\n') + (outbox.length ? '\n' : ''));

let clock = { offset: 0, rtt: null };

/** Реєстр ідентичності: або приймаємо чужий, або створюємо, якщо сесія порожня. */
let registry = null;
let registryAsked = false;

/** Чужі commit-и зберігаються до підтвердження bridge. Інакше рестарт daemon
 *  після просування lastGseq назавжди втратив би ще не застосовану подію. */
let pendingApply = [];
if (existsSync(pendingPath)) {
  pendingApply = readFileSync(pendingPath, 'utf8')
    .split('\n')
    .filter(Boolean)
    .map((line) => {
      try { return JSON.parse(line); } catch { return null; }
    })
    .filter((event) => event && Number.isSafeInteger(event.gseq));
  if (pendingApply.length) log(`відновлено ${pendingApply.length} незастосованих подій для bridge`);
}
const pendingSent = new Set();
const savePending = () =>
  writeFileSync(pendingPath, pendingApply.map((event) => JSON.stringify(event)).join('\n') +
    (pendingApply.length ? '\n' : ''));

const filesync = new FileSync({
  send: (msg) => {
    if (connected) ws.send(JSON.stringify(msg));
  },
  log,
});
// За замовчуванням теку беремо зі snapshot bridge; --project перекриває вручну
// (потрібно, якщо сет ще не збережено -- тоді file_path порожній).
// Сам виклик -- унизу, після оголошення `connected`: сканування одразу анонсує
// маніфест, а це чіпає стан з'єднання.
const PROJECT = arg('project', null);

// ---------------------------------------------------------------------- UDP

const udp = createSocket('udp4');
let bridgeAlive = false;
let bridgeLastSeen = 0;

function toBridge(msg) {
  const buf = Buffer.from(JSON.stringify(msg), 'utf8');
  udp.send(buf, PORT_OUT, '127.0.0.1', (e) => {
    if (e) log('udp send failed:', e.message);
  });
}

udp.on('message', (buf) => {
  let msg;
  try {
    msg = JSON.parse(buf.toString('utf8'));
  } catch (e) {
    return log('від bridge прийшов не-JSON:', e.message);
  }
  bridgeLastSeen = Date.now();

  switch (msg.m) {
    case 'hello':
      bridgeAlive = true;
      pendingSent.clear();
      log(`bridge підключився: Live ${msg.live}, script ${msg.script}, pid ${msg.pid}`);
      bridgeInfo = {
        live: msg.live, script: msg.script, events: msg.events || [], features: msg.features || [],
      };
      announceCapabilities();
      registryAsked = false; // Live перезавантажився -- його реєстр треба відновити
      bootstrapRegistry();
      drainPending();
      break;
    case 'bye':
      bridgeAlive = false;
      pendingSent.clear();
      bridgeInfo = null;
      log('bridge відключився');
      break;
    case 'heartbeat':
      if (!bridgeAlive) {
        bridgeAlive = true;
        log('bridge знову на звʼязку');
        // Спершу з'ясовуємо, чи вміє саме цей екземпляр bridge apply_ack.
        // Інакше capability від попереднього Live дасть хибну гарантію доставки.
        if (!bridgeInfo) toBridge({ m: 'hello_request' });
        // Live міг працювати ще до старту daemon -- тоді hello вже не буде,
        // і без цього виклику сесія лишиться без реєстру назавжди
        bootstrapRegistry();
        if (bridgeInfo) drainPending();
      }
      break;
    case 'event':
      submit(msg.type, msg.payload);
      break;
    case 'snapshot':
      // фаза 1: снапшот лише для діагностики, не для реконструкції стану
      log(`snapshot: tempo=${msg.state?.tempo} playing=${msg.state?.playing} ` +
          `tracks=${msg.state?.tracks?.length} scenes=${msg.state?.scenes?.length}`);
      if (!PROJECT) filesync.setProjectFile(msg.state?.file_path);
      reportSamples(msg.state?.samples);
      break;
    case 'registry':
      // bridge згенерував uuid для своїх об'єктів -- кладемо їх у журнал
      log(`bridge створив реєстр: ${msg.registry?.tracks?.length} треків, ` +
          `${msg.registry?.scenes?.length} сцен, ` +
          `${msg.registry?.aux_tracks?.length || 0} Return/Master, ` +
          `${msg.registry?.chains?.length || 0} Rack chains`);
      submit('RegistryInit', msg.registry);
      break;
    case 'apply_ack': {
      const gseq = Number(msg.gseq);
      pendingSent.delete(gseq);
      if (!msg.ok) {
        log(`bridge не підтвердив застосування #${gseq}: ${msg.error || 'невідома помилка'}`);
        break;
      }
      const before = pendingApply.length;
      pendingApply = pendingApply.filter((event) => event.gseq !== gseq);
      if (pendingApply.length !== before) savePending();
      break;
    }
    case 'log':
      log(`[bridge/${msg.level}] ${msg.text}`);
      break;
  }
});

udp.on('error', (e) => {
  log('UDP error:', e.message);
  udp.close();
  process.exit(1);
});

udp.bind(PORT_IN, '127.0.0.1', () => {
  log(`слухаю bridge на udp://127.0.0.1:${PORT_IN}, відповідаю на :${PORT_OUT}`);
  toBridge({ m: 'snapshot_request' });
});

// ---------------------------------------------------------------- WebSocket

let ws = null;
let connected = false;
let backoff = RECONNECT_MIN;
let clockTimer = null;

function connect() {
  log(`підключаюсь до ${RELAY} як ${AUTHOR}/${SESSION}...`);
  ws = new WebSocket(RELAY);

  ws.on('open', () => {
    connected = true;
    backoff = RECONNECT_MIN;
    ws.send(JSON.stringify({ m: 'join', session: SESSION, author: AUTHOR, since: state.lastGseq, proto: PROTO }));
  });

  ws.on('message', (raw) => {
    const t3 = Date.now() / 1000;
    let msg;
    try {
      msg = JSON.parse(raw);
    } catch {
      return;
    }

    switch (msg.m) {
      case 'welcome':
        log(`relay: head=${msg.head.gseq}, у сесії ${msg.peers.join(', ') || '—'}`);
        registry = msg.registry || null;
        flushOutbox();
        pingClock();
        bootstrapRegistry();
        filesync.announce(); // хай партнер одразу знає, що в нас є
        announceCapabilities();
        break;
      case 'commit':
        onCommit(msg.event);
        break;
      case 'peers':
        log(`у сесії: ${msg.peers.join(', ') || '—'}`);
        break;
      case 'pong': {
        clock = {
          offset: (msg.t1 - msg.t0 + (msg.t2 - t3)) / 2,
          rtt: t3 - msg.t0 - (msg.t2 - msg.t1),
        };
        log(`clock: offset=${(clock.offset * 1000).toFixed(1)}ms rtt=${(clock.rtt * 1000).toFixed(1)}ms`);
        break;
      }
      case 'files_manifest':
        filesync.onManifest(msg.files);
        break;
      case 'file_request':
        filesync.onRequest(msg.path);
        break;
      case 'file_chunk':
        filesync.onChunk(msg);
        break;
      case 'compat':
        log(`НЕСУМІСНІСТЬ: ${msg.text}`);
        break;
      case 'error':
        log(`relay error [${msg.code}]: ${msg.text}`);
        break;
    }
  });

  ws.on('close', () => {
    connected = false;
    if (clockTimer) clearInterval(clockTimer);
    log(`звʼязок з relay втрачено; локальна робота триває, буфер: ${outbox.length}`);
    setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, RECONNECT_MAX);
  });

  ws.on('error', (e) => log('relay:', e.message));
}

function pingClock() {
  const send = () => connected && ws.send(JSON.stringify({ m: 'ping', t0: Date.now() / 1000 }));
  send();
  if (clockTimer) clearInterval(clockTimer);
  clockTimer = setInterval(send, CLOCK_PING_SEC * 1000);
}

// --------------------------------------------------------------------- flow

let bridgeInfo = null;

/** Relay звіряє це між учасниками: подія, якої приймальний бік не знає, доходить
 *  і мовчки нічого не робить -- зовні це виглядає як "sync не працює". */
function announceCapabilities() {
  if (!connected || !bridgeInfo) return;
  ws.send(JSON.stringify({ m: 'client_info', ...bridgeInfo }));
}

let samplesWarned = '';

/** Проєкт із зовнішніми семплами непереносимий: у партнера цих шляхів немає.
 *  Виправити це можна лише в Live (Collect All and Save), тож завдання daemon --
 *  сказати про це виразно й один раз, поки картина не змінилась. */
function reportSamples(s) {
  if (!s || !s.total) return;
  const key = `${s.external?.length}/${s.missing?.length}/${s.total}`;
  if (key === samplesWarned) return;
  samplesWarned = key;

  const ext = s.external || [];
  const miss = s.missing || [];
  if (!ext.length && !miss.length) {
    log(`семпли: ${s.total} — усі всередині проєкту, синхронізуються`);
    return;
  }
  if (ext.length) {
    log(`УВАГА: ${ext.length} з ${s.total} семплів лежать ПОЗА текою проєкту.`);
    log('  Партнер їх не отримає, і посилання в .als у нього не резолвиться.');
    log('  Лікується в Live: File > Collect All and Save.');
    for (const p of ext.slice(0, 5)) log(`  поза проєктом: ${p}`);
  }
  if (miss.length) {
    log(`УВАГА: ${miss.length} семплів взагалі немає на диску (missing media):`);
    for (const p of miss.slice(0, 5)) log(`  бракує: ${p}`);
  }
}

/** Або віддаємо bridge готовий реєстр сесії, або просимо створити новий. */
function bootstrapRegistry() {
  if (!bridgeAlive || !connected || registryAsked) return;
  registryAsked = true;
  if (registry) {
    log('віддаю bridge реєстр сесії');
    toBridge({ m: 'registry_adopt', registry });
  } else {
    log('сесія без реєстру — прошу bridge створити');
    toBridge({ m: 'registry_build' });
  }
}

/** Викликати ЛИШЕ після bootstrapRegistry: події адресуються uuid, тож bridge
 *  має спершу отримати реєстр, інакше жодна з них не зарезолвиться. */
function drainPending() {
  if (!bridgeAlive || !pendingApply.length) return;
  const q = pendingApply.filter((event) => !pendingSent.has(event.gseq));
  if (!q.length) return;
  log(`застосовую ${q.length} відкладених подій`);
  for (const ev of q) {
    pendingSent.add(ev.gseq);
    toBridge({ m: 'apply', type: ev.type, payload: ev.payload, gseq: ev.gseq });
  }

  // Bridge до 0.11.0 не знає apply_ack. Для нього лишаємо стару семантику
  // "UDP-відправка = доставлено", але явно показуємо слабшу гарантію в логах.
  if (!bridgeInfo?.features?.includes('apply_ack')) {
    log('УВАГА: bridge без apply_ack — pending очищено без підтвердження застосування');
    const sent = new Set(q.map((event) => event.gseq));
    pendingApply = pendingApply.filter((event) => !sent.has(event.gseq));
    for (const gseq of sent) pendingSent.delete(gseq);
    savePending();
  }
}

function submit(type, payload) {
  state.lseq += 1;
  saveState();
  const event = { type, payload: payload ?? {}, author: AUTHOR, lseq: state.lseq, ts: Date.now() / 1000 };
  outbox.push(event);
  saveOutbox();
  if (connected) flushOutbox();
  else log(`офлайн, у буфер: ${type} (lseq=${event.lseq})`);
}

function flushOutbox() {
  if (!connected || !outbox.length) return;
  for (const event of outbox) ws.send(JSON.stringify({ m: 'submit', event }));
  // не чистимо тут: подія вважається доставленою тільки коли повернеться як commit
}

function onCommit(ev) {
  const mine = ev.author === AUTHOR;
  if (ev.gseq <= state.lastGseq) {
    // Idempotent ack на ре-сабміт: lastGseq уже міг зберегтися до падіння,
    // а outbox — ще ні. Старий commit все одно має завершити локальну доставку.
    if (mine) {
      const before = outbox.length;
      outbox = outbox.filter((event) => event.lseq !== ev.lseq);
      if (outbox.length !== before) {
        saveOutbox();
        log(`#${ev.gseq} ${ev.type} (повторний ack, outbox очищено)`);
      }
    }
    return;
  }
  state.lastGseq = ev.gseq;
  saveState();
  appendFileSync(localJournalPath, JSON.stringify(ev) + '\n');

  if (ev.type === 'RegistryInit') {
    // Однаковий шлях для автора і для партнера: реєстр канонічний той,
    // що ліг у журнал, а не той, що згенерував локальний bridge.
    registry = ev.payload;
    outbox = outbox.filter((e) => e.type !== 'RegistryInit');
    saveOutbox();
    log(`#${ev.gseq} RegistryInit від ${ev.author} — віддаю bridge`);
    if (bridgeAlive) toBridge({ m: 'registry_adopt', registry });
    return;
  }

  if (mine) {
    const before = outbox.length;
    outbox = outbox.filter((e) => e.lseq !== ev.lseq);
    if (outbox.length !== before) saveOutbox();
    // власна дія вже застосована в Live оптимістично -- не переграємо
    log(`#${ev.gseq} ${ev.type} (моя, ack)`);
    return;
  }

  log(`#${ev.gseq} ${ev.type} ${JSON.stringify(ev.payload)} <- ${ev.author}`);
  pendingApply.push(ev);
  savePending();
  drainPending();
}

// -------------------------------------------------------------------- watch

// Періодичне сканування замість fs.watch: тека проєкту може лежати в OneDrive,
// де події файлової системи приходять із затримкою або не приходять зовсім.
setInterval(() => filesync.rescan(), 10_000);

setInterval(() => {
  if (bridgeAlive && Date.now() - bridgeLastSeen > 6000) {
    bridgeAlive = false;
    pendingSent.clear();
    bridgeInfo = null;
    log('bridge замовк (Live закритий або скрипт не вибраний у Preferences)');
  }
}, 3000);

process.on('SIGINT', () => {
  log('зупиняюсь');
  try {
    udp.close();
    ws?.close();
  } catch {}
  process.exit(0);
});

log(`daemon ${AUTHOR} стартував`);
if (PROJECT) filesync.setProjectRoot(PROJECT);
connect();
