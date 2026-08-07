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
const statePath = join(STATE_DIR, `${AUTHOR}.json`);
const outboxPath = join(STATE_DIR, `${AUTHOR}.outbox.jsonl`);
const localJournalPath = join(STATE_DIR, `${AUTHOR}.applied.jsonl`);

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
      log(`bridge підключився: Live ${msg.live}, script ${msg.script}, pid ${msg.pid}`);
      break;
    case 'bye':
      bridgeAlive = false;
      log('bridge відключився');
      break;
    case 'heartbeat':
      if (!bridgeAlive) {
        bridgeAlive = true;
        log('bridge знову на звʼязку');
      }
      break;
    case 'event':
      submit(msg.type, msg.payload);
      break;
    case 'snapshot':
      // фаза 1: снапшот лише для діагностики, не для реконструкції стану
      log(`snapshot: tempo=${msg.state?.tempo} playing=${msg.state?.playing} ` +
          `tracks=${msg.state?.tracks?.length} scenes=${msg.state?.scenes?.length}`);
      break;
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
        flushOutbox();
        pingClock();
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
  if (ev.gseq <= state.lastGseq) return; // вже бачили
  state.lastGseq = ev.gseq;
  saveState();
  appendFileSync(localJournalPath, JSON.stringify(ev) + '\n');

  const mine = ev.author === AUTHOR;
  if (mine) {
    const before = outbox.length;
    outbox = outbox.filter((e) => e.lseq !== ev.lseq);
    if (outbox.length !== before) saveOutbox();
    // власна дія вже застосована в Live оптимістично -- не переграємо
    log(`#${ev.gseq} ${ev.type} (моя, ack)`);
    return;
  }

  log(`#${ev.gseq} ${ev.type} ${JSON.stringify(ev.payload)} <- ${ev.author}`);
  if (!bridgeAlive) {
    log('  ...bridge офлайн, подія в Live не застосована');
    return;
  }
  toBridge({ m: 'apply', type: ev.type, payload: ev.payload, gseq: ev.gseq });
}

// -------------------------------------------------------------------- watch

setInterval(() => {
  if (bridgeAlive && Date.now() - bridgeLastSeen > 6000) {
    bridgeAlive = false;
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
connect();
