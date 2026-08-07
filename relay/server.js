// Relay / sequencer -- єдине джерело порядку подій.
//
// Не знає нічого про Live і про LOM. Приймає семантичні події, присвоює
// монотонний global_seq, зшиває їх у hash-chain, пише в журнал, роздає всім.

import { createServer } from 'node:http';
import { createHash } from 'node:crypto';
import { appendFileSync, readFileSync, existsSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { WebSocketServer } from 'ws';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.MP_RELAY_PORT || 19870);
const PROTO = 1;
const JOURNAL_DIR = process.env.MP_JOURNAL_DIR || join(__dirname, 'journals');

// ---------------------------------------------------------------- canonical

/** Детермінована серіалізація: ключі відсортовані, без пробілів. */
function canonical(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  const keys = Object.keys(value).sort();
  return '{' + keys.map((k) => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
}

function hashEvent(prevHash, body) {
  return createHash('sha256').update(prevHash + canonical(body)).digest('hex');
}

// ------------------------------------------------------------------ session

class Session {
  constructor(name) {
    this.name = name;
    this.journal = [];
    this.registry = null;  // payload першого RegistryInit; далі він незмінний
    this.seen = new Set(); // "author:lseq" -- дедуплікація ре-сабмітів
    this.clients = new Set();
    this.path = join(JOURNAL_DIR, `${name}.jsonl`);
    this.#load();
  }

  get head() {
    const last = this.journal[this.journal.length - 1];
    return last ? { gseq: last.gseq, hash: last.hash } : { gseq: 0, hash: '' };
  }

  #load() {
    if (!existsSync(this.path)) return;
    const lines = readFileSync(this.path, 'utf8').split('\n').filter(Boolean);
    let prevHash = '';
    for (const line of lines) {
      let ev;
      try {
        ev = JSON.parse(line);
      } catch {
        log(`[${this.name}] пошкоджений рядок журналу, зупиняюсь на gseq=${this.head.gseq}`);
        break;
      }
      const { hash, prev_hash: prev, ...body } = ev;
      if (prev !== prevHash || hashEvent(prev, body) !== hash) {
        log(`[${this.name}] hash-chain розірвано на gseq=${ev.gseq}; хвіст відкинуто`);
        break;
      }
      this.journal.push(ev);
      this.seen.add(`${ev.author}:${ev.lseq}`);
      if (ev.type === 'RegistryInit' && !this.registry) this.registry = ev.payload;
      prevHash = hash;
    }
    log(`[${this.name}] журнал відновлено: ${this.journal.length} подій, head=${this.head.gseq}`);
  }

  /** Повертає закомічену подію, або null якщо це дублікат. */
  commit({ type, payload, author, lseq, ts }) {
    const dedupeKey = `${author}:${lseq}`;
    if (this.seen.has(dedupeKey)) return null;

    // Реєстр ідентичності створюється один раз за сесію. Так вирішується гонка
    // одночасного конекту двох гравців: обидва бачать порожній журнал, обидва
    // сабмітять RegistryInit, у журнал лягає перший -- і другий приймає його
    // як звичайний commit. Власника сесії тримати не треба.
    if (type === 'RegistryInit' && this.registry) {
      log(`[${this.name}] повторний RegistryInit від ${author} відхилено`);
      return null;
    }

    const prev = this.head;
    const body = {
      gseq: prev.gseq + 1,
      type,
      payload: payload ?? {},
      author,
      lseq,
      ts: ts ?? null,
      srv_ts: Date.now() / 1000,
    };
    const ev = { ...body, prev_hash: prev.hash, hash: hashEvent(prev.hash, body) };

    this.journal.push(ev);
    this.seen.add(dedupeKey);
    if (type === 'RegistryInit') this.registry = ev.payload;
    try {
      appendFileSync(this.path, JSON.stringify(ev) + '\n');
    } catch (e) {
      log(`[${this.name}] НЕ ВДАЛОСЬ записати журнал: ${e.message}`);
    }
    return ev;
  }

  tailSince(gseq) {
    return this.journal.filter((e) => e.gseq > gseq);
  }

  peers() {
    return [...this.clients].map((c) => c.author).filter(Boolean);
  }

  broadcast(msg, except = null) {
    const raw = JSON.stringify(msg);
    for (const c of this.clients) {
      if (c === except) continue;
      if (c.ws.readyState === 1) c.ws.send(raw);
    }
  }
}

const sessions = new Map();
function getSession(name) {
  if (!sessions.has(name)) sessions.set(name, new Session(name));
  return sessions.get(name);
}

// --------------------------------------------------------------------- wire

function log(...args) {
  // локальний час, щоб збігалося з bridge.log при звірянні логів
  console.log(new Date().toTimeString().slice(0, 8), ...args);
}

mkdirSync(JOURNAL_DIR, { recursive: true });

const http = createServer((req, res) => {
  if (req.url === '/health') {
    const body = [...sessions.values()].map((s) => ({
      session: s.name,
      head: s.head.gseq,
      peers: s.peers(),
    }));
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ ok: true, proto: PROTO, sessions: body }, null, 2));
    return;
  }
  res.writeHead(404).end();
});

const wss = new WebSocketServer({ server: http });

wss.on('connection', (ws) => {
  const client = { ws, author: null, session: null };

  const send = (msg) => {
    if (ws.readyState === 1) ws.send(JSON.stringify(msg));
  };

  ws.on('message', (raw) => {
    const t1 = Date.now() / 1000;
    let msg;
    try {
      msg = JSON.parse(raw);
    } catch {
      return send({ m: 'error', code: 'bad_json', text: 'не JSON' });
    }

    switch (msg.m) {
      case 'join': {
        if (msg.proto !== PROTO) {
          send({ m: 'error', code: 'proto_mismatch', text: `relay говорить proto ${PROTO}` });
          return ws.close();
        }
        if (!msg.author) {
          send({ m: 'error', code: 'no_author', text: 'author обовʼязковий' });
          return ws.close();
        }
        const session = getSession(msg.session || 'default');
        client.author = String(msg.author);
        client.session = session;
        session.clients.add(client);

        // registry віддаємо окремо від хвоста журналу: клієнт після рестарту
        // приходить з високим since і хвіст RegistryInit уже не містить
        send({
          m: 'welcome',
          author: client.author,
          session: session.name,
          head: session.head,
          peers: session.peers(),
          registry: session.registry,
        });
        for (const ev of session.tailSince(Number(msg.since) || 0)) send({ m: 'commit', event: ev });
        session.broadcast({ m: 'peers', peers: session.peers() });
        log(`[${session.name}] + ${client.author} (${session.clients.size} онлайн, since=${msg.since || 0})`);
        break;
      }

      case 'submit': {
        if (!client.session) return send({ m: 'error', code: 'not_joined', text: 'спершу join' });
        const e = msg.event || {};
        if (!e.type || typeof e.lseq !== 'number') {
          return send({ m: 'error', code: 'bad_event', text: 'потрібні type і lseq' });
        }
        const ev = client.session.commit({ ...e, author: client.author });
        if (!ev) break; // дублікат після реконнекту -- тихо ігноруємо
        client.session.broadcast({ m: 'commit', event: ev }); // включно з автором
        log(`[${client.session.name}] #${ev.gseq} ${ev.type} ${JSON.stringify(ev.payload)} <- ${ev.author}`);
        break;
      }

      // File Sync Layer: незалежний від журналу (vision.md §4). Relay тут --
      // тупа труба між учасниками, нічого не комітить і нічого не пам'ятає.
      case 'files_manifest':
      case 'file_request':
      case 'file_chunk': {
        if (!client.session) return send({ m: 'error', code: 'not_joined', text: 'спершу join' });
        client.session.broadcast({ ...msg, from: client.author }, client);
        break;
      }

      case 'ping':
        send({ m: 'pong', t0: msg.t0, t1, t2: Date.now() / 1000 });
        break;

      default:
        send({ m: 'error', code: 'unknown_msg', text: String(msg.m) });
    }
  });

  ws.on('close', () => {
    if (!client.session) return;
    client.session.clients.delete(client);
    client.session.broadcast({ m: 'peers', peers: client.session.peers() });
    log(`[${client.session.name}] - ${client.author} (${client.session.clients.size} онлайн)`);
  });

  ws.on('error', (e) => log('ws error:', e.message));
});

http.listen(PORT, () => {
  log(`relay слухає ws://0.0.0.0:${PORT} (proto ${PROTO}), журнали в ${JOURNAL_DIR}`);
});
