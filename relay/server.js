// Relay / sequencer -- єдине джерело порядку подій.
//
// Не знає нічого про Live і про LOM. Приймає семантичні події, присвоює
// монотонний global_seq, зшиває їх у hash-chain, пише в журнал, роздає всім.

import { createServer } from 'node:http';
import { createHash } from 'node:crypto';
import {
  appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync,
} from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { WebSocketServer } from 'ws';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.MP_RELAY_PORT || 19870);
const PROTO = 1;
const JOURNAL_DIR = process.env.MP_JOURNAL_DIR || join(__dirname, 'journals');
const MAX_WS_PAYLOAD = 2 * 1024 * 1024;

function validSessionName(value) {
  return typeof value === 'string' && value.length > 0 && value.length <= 128 &&
    value !== '.' && value !== '..' && !/[<>:"/\\|?*\u0000-\u001f]/.test(value) && !/[ .]$/.test(value);
}

function validAuthor(value) {
  return typeof value === 'string' && value.length > 0 && value.length <= 128 &&
    !/[\u0000-\u001f]/.test(value);
}

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
    this.seen = new Map(); // "author:lseq" -> commit для idempotent ack ре-сабмітів
    this.clients = new Set();
    this.loadError = null;
    this.checkpointError = null;
    this.path = join(JOURNAL_DIR, `${name}.jsonl`);
    this.checkpointPath = join(JOURNAL_DIR, `${name}.checkpoint.json`);
    this.#load();
    if (!this.loadError) this.#verifyCheckpoint();
  }

  get head() {
    const last = this.journal[this.journal.length - 1];
    return last ? { gseq: last.gseq, hash: last.hash } : { gseq: 0, hash: '' };
  }

  #load() {
    if (!existsSync(this.path)) return;
    let lines;
    try {
      lines = readFileSync(this.path, 'utf8').split('\n').filter(Boolean);
    } catch (e) {
      this.loadError = `журнал не читається: ${e.message}`;
      log(`[${this.name}] ${this.loadError}`);
      return;
    }
    let prevHash = '';
    for (const line of lines) {
      let ev;
      try {
        ev = JSON.parse(line);
      } catch {
        this.loadError = `пошкоджений рядок після gseq=${this.head.gseq}`;
        log(`[${this.name}] ${this.loadError}; нові commit заблоковано`);
        break;
      }
      const { hash, prev_hash: prev, ...body } = ev;
      const expectedGseq = this.journal.length + 1;
      if (ev.gseq !== expectedGseq) {
        this.loadError = `gseq=${ev.gseq} замість ${expectedGseq}`;
        log(`[${this.name}] ${this.loadError}; нові commit заблоковано`);
        break;
      }
      if (prev !== prevHash || hashEvent(prev, body) !== hash) {
        this.loadError = `hash-chain розірвано на gseq=${ev.gseq}`;
        log(`[${this.name}] ${this.loadError}; нові commit заблоковано`);
        break;
      }
      this.journal.push(ev);
      this.seen.set(`${ev.author}:${ev.lseq}`, ev);
      if (ev.type === 'RegistryInit' && !this.registry) this.registry = ev.payload;
      prevHash = hash;
    }
    log(`[${this.name}] журнал відновлено: ${this.journal.length} подій, head=${this.head.gseq}`);
  }

  #verifyCheckpoint() {
    if (existsSync(this.checkpointPath)) {
      let checkpoint;
      try {
        checkpoint = JSON.parse(readFileSync(this.checkpointPath, 'utf8'));
      } catch (error) {
        this.loadError = `checkpoint не читається: ${error.message}`;
        log(`[${this.name}] ${this.loadError}; нові commit заблоковано`);
        return;
      }
      if (checkpoint.version !== 1 || !Number.isSafeInteger(checkpoint.gseq) ||
          checkpoint.gseq < 0 || typeof checkpoint.hash !== 'string') {
        this.loadError = 'checkpoint має некоректний формат';
        log(`[${this.name}] ${this.loadError}; нові commit заблоковано`);
        return;
      }
      if (checkpoint.gseq > this.head.gseq) {
        this.loadError = `checkpoint попереду журналу: ${checkpoint.gseq} > ${this.head.gseq}`;
        log(`[${this.name}] ${this.loadError}; нові commit заблоковано`);
        return;
      }
      const anchoredHash = checkpoint.gseq === 0 ? '' : this.journal[checkpoint.gseq - 1]?.hash;
      if (checkpoint.hash !== anchoredHash) {
        this.loadError = `checkpoint не збігається з журналом на gseq=${checkpoint.gseq}`;
        log(`[${this.name}] ${this.loadError}; нові commit заблоковано`);
        return;
      }
      if (checkpoint.gseq === this.head.gseq && checkpoint.hash === this.head.hash) return;
      log(`[${this.name}] checkpoint відстає (${checkpoint.gseq} < ${this.head.gseq}), оновлюю`);
    }
    this.#writeCheckpoint();
  }

  #writeCheckpoint() {
    const checkpoint = {
      version: 1,
      session: this.name,
      gseq: this.head.gseq,
      hash: this.head.hash,
      updated_at: Date.now() / 1000,
    };
    const tmp = `${this.checkpointPath}.${process.pid}.tmp`;
    try {
      writeFileSync(tmp, JSON.stringify(checkpoint) + '\n');
      renameSync(tmp, this.checkpointPath);
      this.checkpointError = null;
      return true;
    } catch (error) {
      try { rmSync(tmp, { force: true }); } catch {}
      this.checkpointError = error.message;
      log(`[${this.name}] НЕ ВДАЛОСЬ записати checkpoint: ${error.message}`);
      return false;
    }
  }

  /** Повертає commit і ознаку дубліката; null лише для відхиленого RegistryInit. */
  commit({ type, payload, author, lseq, ts }) {
    if (this.loadError) throw new Error(this.loadError);
    const dedupeKey = `${author}:${lseq}`;
    if (this.seen.has(dedupeKey)) return { event: this.seen.get(dedupeKey), duplicate: true };

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

    // Commit існує лише після успішного запису. Інакше клієнти побачили б подію,
    // якої вже не буде в журналі після рестарту relay.
    appendFileSync(this.path, JSON.stringify(ev) + '\n');
    this.journal.push(ev);
    this.seen.set(dedupeKey, ev);
    if (type === 'RegistryInit') this.registry = ev.payload;
    this.#writeCheckpoint();
    return { event: ev, duplicate: false };
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
      journal_error: s.loadError,
      checkpoint_error: s.checkpointError,
    }));
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ ok: true, proto: PROTO, sessions: body }, null, 2));
    return;
  }
  res.writeHead(404).end();
});

const wss = new WebSocketServer({ server: http, maxPayload: MAX_WS_PAYLOAD });

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
        if (client.session) {
          send({ m: 'error', code: 'already_joined', text: 'join уже виконано' });
          return ws.close();
        }
        if (msg.proto !== PROTO) {
          send({ m: 'error', code: 'proto_mismatch', text: `relay говорить proto ${PROTO}` });
          return ws.close();
        }
        const author = String(msg.author ?? '');
        if (!validAuthor(author)) {
          send({ m: 'error', code: 'bad_author', text: 'author має містити 1–128 друкованих символів' });
          return ws.close();
        }
        const sessionName = msg.session || 'default';
        if (!validSessionName(sessionName)) {
          send({ m: 'error', code: 'bad_session', text: 'неприпустима назва сесії' });
          return ws.close();
        }
        const session = getSession(sessionName);
        client.author = author;
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
        if (typeof e.type !== 'string' || !e.type || e.type.length > 64 ||
            !Number.isSafeInteger(e.lseq) || e.lseq < 0) {
          return send({ m: 'error', code: 'bad_event', text: 'потрібні type і lseq' });
        }
        let result;
        try {
          result = client.session.commit({ ...e, author: client.author });
        } catch (error) {
          log(`[${client.session.name}] НЕ ВДАЛОСЬ записати журнал: ${error.message}`);
          return send({ m: 'error', code: 'journal_write_failed', text: 'подію не закомічено' });
        }
        if (!result) break; // повторний RegistryInit відхилено
        const { event: ev, duplicate } = result;
        if (duplicate) {
          // Автор міг отримати commit, записати lastGseq і впасти до очищення outbox.
          // Повторна відповідь тим самим commit робить цей recovery idempotent.
          send({ m: 'commit', event: ev });
          log(`[${client.session.name}] duplicate ack #${ev.gseq} ${ev.type} -> ${ev.author}`);
          break;
        }
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

      // Розсинхрон версій (vision.md §8) виглядає як "sync не працює": подія
      // доходить, а приймальний бік про такий тип не знає і мовчки її ковтає.
      // Тут це стає видимим одразу при конекті, а не після звірки двох логів.
      case 'client_info': {
        if (!client.session) return send({ m: 'error', code: 'not_joined', text: 'спершу join' });
        client.info = { live: msg.live, script: msg.script, events: msg.events || [] };
        for (const other of client.session.clients) {
          if (other === client || !other.info) continue;

          if (other.info.script !== client.info.script) {
            const t = `версії скрипта різні: ${client.author}=${client.info.script}, ${other.author}=${other.info.script}`;
            log(`[${client.session.name}] ${t}`);
            client.session.broadcast({ m: 'compat', text: t });
          }
          if (other.info.live !== client.info.live) {
            log(`[${client.session.name}] версії Live різні: ${client.author}=${client.info.live}, ${other.author}=${other.info.live}`);
          }

          const mine = new Set(client.info.events);
          const theirs = new Set(other.info.events);
          const gapThem = client.info.events.filter((e) => !theirs.has(e));
          const gapMe = other.info.events.filter((e) => !mine.has(e));
          for (const [who, gap] of [[other.author, gapThem], [client.author, gapMe]]) {
            if (!gap.length) continue;
            const t = `${who} не вміє застосовувати: ${gap.join(', ')} — ці події в нього не спрацюють`;
            log(`[${client.session.name}] ${t}`);
            client.session.broadcast({ m: 'compat', text: t });
          }
        }
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
