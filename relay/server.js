// Relay / sequencer -- єдине джерело порядку подій.
//
// Не знає нічого про Live і про LOM. Приймає семантичні події, присвоює
// монотонний global_seq, зшиває їх у hash-chain, пише в журнал, роздає всім.

import { createServer } from 'node:http';
import { createHash, timingSafeEqual } from 'node:crypto';
import {
  appendFileSync, closeSync, existsSync, fsyncSync, mkdirSync, openSync, readdirSync,
  readFileSync, renameSync, rmSync, statSync, writeFileSync, writeSync,
} from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { WebSocketServer } from 'ws';
import { compactTail } from './compact.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.MP_RELAY_PORT || 19870);
const PROTO = 1;
const JOURNAL_DIR = process.env.MP_JOURNAL_DIR || join(__dirname, 'journals');
const MAX_WS_PAYLOAD = 2 * 1024 * 1024;
// Обрив мережі (вимкнений Wi-Fi, приспана машина) не закриває TCP-сокет: без
// цього мертвий клієнт лишався б у peers і в /health до системного таймауту.
// Пінг тримає з'єднання живим, тиша довша за STALE_SEC вважається смертю.
const HEARTBEAT_SEC = Number(process.env.MP_HEARTBEAT_SEC || 15);
const STALE_SEC = Number(process.env.MP_STALE_SEC || 45);
// Хвіст на join стискається (compact.js). MP_COMPACT_JOIN=0 -- аварійний вимикач:
// клієнт тоді отримує повну історію, як до появи стиснення.
// Фізичне стиснення журналу (vision.md §5 п.7). Стиснутий журнал -- це
// підпослідовність, яка дає той самий стан: базою для нового учасника служить
// не знімок стану Live, а сам .als, який обидві машини відкрили з копії.
// Повна історія при цьому не зникає -- вона переїжджає в холодний архів.
const COMPACT_AT = Number(process.env.MP_COMPACT_AT || 2000);
const COMPACT_MIN_GAIN = 0.2;
const COMPACT_JOIN = process.env.MP_COMPACT_JOIN !== '0';
// Лок -- сесійний стан, а не історія: у журнал він не потрапляє (vision.md §5 п.5).
// І це порада, а не заборона: relay нікому не відмовляє в commit, бо автор уже
// застосував зміну у своєму Live, і відкат вимагав би replay, якого ще немає.
// Лок каже партнеру "цей обʼєкт зараз редагують", не більше.
const LOCK_TTL_SEC = Number(process.env.MP_LOCK_TTL_SEC || 300);
const LOCK_TTL_MAX = 900;
const LOCKS_PER_AUTHOR = 64;
// Присутність: хто на що дивиться. Стан сесії, як локи, але з двома
// відмінностями. Ключ -- автор, а не обʼєкт: конфлікту тут не існує за
// побудовою, тож і арбітражу немає. І TTL немає: лок поновлюється жестом,
// а присутність істинна, поки людина мовчки дивиться на трек -- таймаут
// стер би її посеред роботи. Замість TTL -- смерть разом із зʼєднанням.
const PRESENCE_RATE = Number(process.env.MP_PRESENCE_RATE || 5);
const PRESENCE_MAX_LABEL = 64;

// Токен кімнати. Порожній MP_RELAY_TOKEN -- відкритий relay, як було досі:
// у домашній мережі це нормально, а от у спільному Wi-Fi чи через тунель
// будь-хто інакше може зайти в сесію і писати в журнал.
const TOKEN = process.env.MP_RELAY_TOKEN || '';
// Захист журналу від клієнта, що зациклився: ліміт великий настільки, щоб
// звичайна робота (жест = сотні дрібних подій) його не бачила.
const SUBMIT_RATE = Number(process.env.MP_SUBMIT_RATE || 100);
const SUBMIT_BURST = Number(process.env.MP_SUBMIT_BURST || 300);
// Порожня сесія тримає в памʼяті весь свій журнал і мапу дедуплікації. Після
// такого простою вона нікому не потрібна: журнал лишається на диску, а наступний
// join підніме її наново разом із перевіркою hash-chain.
const SESSION_IDLE_SEC = Number(process.env.MP_SESSION_IDLE_SEC || 900);
// Довговічність запису. Без fsync commit, який клієнт уже побачив, може зникнути
// при зникненні живлення: appendFileSync повертається, коли дані в кеші ОС, а не
// на диску. Вмикається окремо, бо коштує мілісекунди на кожну подію.
const FSYNC = process.env.MP_FSYNC === '1';
// Клієнт, який не читає (заснув ноутбук, зависла машина), інакше роздуває буфер
// сокета файловими чанками, поки heartbeat не помітить тишу.
const MAX_BUFFERED = Number(process.env.MP_MAX_BUFFERED || 16 * 1024 * 1024);

function tokenOk(given) {
  if (!TOKEN) return true;
  // Порівнюємо хеші: однакова довжина для timingSafeEqual і жодного
  // натяку на довжину справжнього токена.
  const mine = createHash('sha256').update(TOKEN).digest();
  const theirs = createHash('sha256').update(String(given ?? '')).digest();
  return timingSafeEqual(mine, theirs);
}

/** Token bucket на зʼєднання: сплеск дозволений, усталений темп -- ні. */
function allowSubmit(client, now) {
  client.budget = Math.min(SUBMIT_BURST, client.budget + (now - client.refilledAt) * SUBMIT_RATE);
  client.refilledAt = now;
  if (client.budget < 1) return false;
  client.budget -= 1;
  return true;
}

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
    this.locks = new Map(); // object -> {object, label, author, since, expires}
    this.presence = new Map(); // author -> {author, view, following, updated_at}
    this.emptySince = null;
    this.lastLseq = new Map(); // author -> максимальний закомічений lseq
    this.archivedThrough = 0;
    this.compacted = false;
    this.compactError = null;
    this.nextCompactAt = COMPACT_AT;
    this.servedEvents = 0;
    this.droppedEvents = 0;
    this.loadError = null;
    this.checkpointError = null;
    this.path = join(JOURNAL_DIR, `${name}.jsonl`);
    this.checkpointPath = join(JOURNAL_DIR, `${name}.checkpoint.json`);
    this.archivePath = join(JOURNAL_DIR, `${name}.archive.jsonl`);
    this.checkpoint = this.#readCheckpoint();
    this.#load();
    if (!this.loadError) this.#verifyCheckpoint();
  }

  get head() {
    const last = this.journal[this.journal.length - 1];
    return last ? { gseq: last.gseq, hash: last.hash } : { gseq: 0, hash: '' };
  }

  #readCheckpoint() {
    if (!existsSync(this.checkpointPath)) return null;
    try {
      return JSON.parse(readFileSync(this.checkpointPath, 'utf8'));
    } catch (error) {
      this.loadError = `checkpoint не читається: ${error.message}`;
      log(`[${this.name}] ${this.loadError}; нові commit заблоковано`);
      return null;
    }
  }

  #load() {
    if (this.loadError || !existsSync(this.path)) return;
    let lines;
    try {
      lines = readFileSync(this.path, 'utf8').split('\n').filter(Boolean);
    } catch (e) {
      this.loadError = `журнал не читається: ${e.message}`;
      log(`[${this.name}] ${this.loadError}`);
      return;
    }

    // Суцільний журнал перевіряється строго: пропущений gseq -- це втрачений
    // рядок. Стиснутий має дірки за побудовою, і там роль контролю бере на себе
    // кількість подій у checkpoint плюс власний хеш кожної події.
    const compacted = this.checkpoint?.compacted === true;

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
      const last = this.journal[this.journal.length - 1];
      const expected = last ? last.gseq + 1 : 1;

      if (!Number.isSafeInteger(ev.gseq) || (compacted ? ev.gseq < expected : ev.gseq !== expected)) {
        this.loadError = `gseq=${ev.gseq} замість ${compacted ? `>=${expected}` : expected}`;
        log(`[${this.name}] ${this.loadError}; нові commit заблоковано`);
        break;
      }
      if (hashEvent(prev, body) !== hash) {
        this.loadError = `подія gseq=${ev.gseq} не збігається зі своїм хешем`;
        log(`[${this.name}] ${this.loadError}; нові commit заблоковано`);
        break;
      }
      // Сусідні за gseq події мають зчеплюватись; через дірку зчеплення
      // перевіряється тільки за архівом, де історія повна.
      const linked = last
        ? (ev.gseq !== last.gseq + 1 || prev === last.hash)
        : (ev.gseq !== 1 || prev === '');
      if (!linked) {
        this.loadError = `hash-chain розірвано на gseq=${ev.gseq}`;
        log(`[${this.name}] ${this.loadError}; нові commit заблоковано`);
        break;
      }

      this.journal.push(ev);
      this.seen.set(`${ev.author}:${ev.lseq}`, ev);
      this.#rememberLseq(ev.author, ev.lseq);
      if (ev.type === 'RegistryInit' && !this.registry) this.registry = ev.payload;
    }

    // Журнал коротший за той, що описаний у checkpoint -- рядки зникли.
    // Довший бути може: процес міг упасти між append і записом checkpoint.
    if (!this.loadError && Number.isSafeInteger(this.checkpoint?.events) &&
        lines.length < this.checkpoint.events) {
      this.loadError = `у журналі ${lines.length} подій замість ${this.checkpoint.events}`;
      log(`[${this.name}] ${this.loadError}; нові commit заблоковано`);
    }

    this.compacted = compacted;
    this.archivedThrough = Number.isSafeInteger(this.checkpoint?.archived_through)
      ? this.checkpoint.archived_through : 0;
    for (const [author, lseq] of Object.entries(this.checkpoint?.authors || {})) {
      if (Number.isSafeInteger(lseq)) this.#rememberLseq(author, lseq);
    }
    this.nextCompactAt = Math.max(COMPACT_AT, this.journal.length + COMPACT_AT);

    log(`[${this.name}] журнал відновлено: ${this.journal.length} подій, head=${this.head.gseq}` +
        (compacted ? `, стиснутий (архів до #${this.archivedThrough})` : ''));
  }

  /** Запис події в журнал; із MP_FSYNC=1 -- аж до диска. */
  #append(line) {
    if (!FSYNC) {
      appendFileSync(this.path, line);
      return;
    }
    const fd = openSync(this.path, 'a');
    try {
      writeSync(fd, line);
      fsyncSync(fd);
    } finally {
      closeSync(fd);
    }
  }

  #rememberLseq(author, lseq) {
    const known = this.lastLseq.get(author);
    if (known === undefined || lseq > known) this.lastLseq.set(author, lseq);
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
      const anchoredHash = checkpoint.gseq === 0
        ? '' : this.journal.find((ev) => ev.gseq === checkpoint.gseq)?.hash;
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
      events: this.journal.length,
      compacted: this.compacted,
      archived_through: this.archivedThrough,
      // Дедуплікація має пережити і рестарт, і стиснення: подія, яку вже
      // викинули з живого журналу, не сміє закомітитись удруге.
      authors: Object.fromEntries(this.lastLseq),
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
    const known = this.lastLseq.get(author);
    if (known !== undefined && lseq <= known) return { acked: true };

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
    this.#append(JSON.stringify(ev) + '\n');
    this.journal.push(ev);
    this.seen.set(dedupeKey, ev);
    if (type === 'RegistryInit') this.registry = ev.payload;
    this.#rememberLseq(author, lseq);
    this.#writeCheckpoint();
    this.#maybeCompact();
    return { event: ev, duplicate: false };
  }

  tailSince(gseq) {
    return this.journal.filter((e) => e.gseq > gseq);
  }

  peers() {
    return [...this.clients].map((c) => c.author).filter(Boolean);
  }

  onlineClients() {
    const now = Date.now() / 1000;
    return [...this.clients].map((c) => ({
      author: c.author,
      ip: c.ip,
      port: c.port,
      connected_at: c.connectedAt,
      connected_sec: Math.max(0, Math.round(now - c.connectedAt)),
      idle_sec: Math.max(0, Math.round(now - c.lastSeen)),
      live: c.info?.live ?? null,
      script: c.info?.script ?? null,
      features: c.info?.features ?? [],
      events: c.info?.events ?? [],
    })).filter((c) => c.author);
  }

  authorStats() {
    const stats = new Map();
    const ensure = (author) => {
      const name = author || '(unknown)';
      if (!stats.has(name)) {
        stats.set(name, {
          author: name,
          online: false,
          ip: null,
          commits: 0,
          actions: 0,
          by_type: {},
          last_gseq: 0,
          last_type: null,
          last_ts: null,
          last_srv_ts: null,
        });
      }
      return stats.get(name);
    };

    for (const ev of this.journal) {
      const s = ensure(ev.author);
      s.commits += 1;
      if (ev.type !== 'RegistryInit') s.actions += 1;
      s.by_type[ev.type] = (s.by_type[ev.type] || 0) + 1;
      s.last_gseq = ev.gseq;
      s.last_type = ev.type;
      s.last_ts = ev.ts ?? null;
      s.last_srv_ts = ev.srv_ts ?? null;
    }

    for (const client of this.clients) {
      const s = ensure(client.author);
      s.online = true;
      s.ip = client.ip;
      s.port = client.port;
      s.live = client.info?.live ?? null;
      s.script = client.info?.script ?? null;
      s.connected_at = client.connectedAt;
    }

    return [...stats.values()].sort((a, b) => {
      if (a.online !== b.online) return a.online ? -1 : 1;
      return a.author.localeCompare(b.author);
    });
  }

  // ------------------------------------------------------------------ локи
  //
  // Лок належить автору, а не зʼєднанню: реконект того самого гравця має
  // підхопити власний лок, а не впертись у нього як у чужий.

  acquireLock(author, object, label, ttl) {
    const now = Date.now() / 1000;
    const held = this.locks.get(object);
    if (held && held.author !== author && held.expires > now) return { ok: false, lock: held };
    const mine = held && held.author === author;
    const lock = {
      object,
      label: label ?? (mine ? held.label : null),
      author,
      since: mine ? held.since : now,
      expires: now + ttl,
    };
    this.locks.set(object, lock);
    return { ok: true, lock };
  }

  releaseLock(author, object) {
    const held = this.locks.get(object);
    if (!held || held.author !== author) return false;
    this.locks.delete(object);
    return true;
  }

  releaseLocksOf(author) {
    let changed = false;
    for (const [object, lock] of this.locks) {
      if (lock.author !== author) continue;
      this.locks.delete(object);
      changed = true;
    }
    return changed;
  }

  lockCountOf(author) {
    let count = 0;
    for (const lock of this.locks.values()) if (lock.author === author) count += 1;
    return count;
  }

  /** Гравець вийшов з утриманим локом (vision.md §7) -- TTL знімає його сам. */
  expireLocks(now = Date.now() / 1000) {
    let changed = false;
    for (const [object, lock] of this.locks) {
      if (lock.expires > now) continue;
      this.locks.delete(object);
      log(`[${this.name}] лок ${object} (${lock.author}) звільнено за таймаутом`);
      changed = true;
    }
    return changed;
  }

  lockList() {
    const now = Date.now() / 1000;
    return [...this.locks.values()].map((lock) => ({
      object: lock.object,
      label: lock.label,
      author: lock.author,
      since: lock.since,
      held_sec: Math.max(0, Math.round(now - lock.since)),
      expires_in: Math.max(0, Math.round(lock.expires - now)),
    }));
  }

  // ------------------------------------------------------------ присутність

  setPresence(author, view, following) {
    this.presence.set(author, {
      author,
      view: trimNames(view),
      following: typeof following === 'string' && following ? following : null,
      updated_at: Date.now() / 1000,
    });
  }

  clearPresence(author) {
    return this.presence.delete(author);
  }

  presenceList() {
    const now = Date.now() / 1000;
    return [...this.presence.values()].map((entry) => ({
      ...entry,
      age_sec: Math.max(0, Math.round(now - entry.updated_at)),
    }));
  }

  /** Стиснення живого журналу: історія їде в архів, на диску лишається
   *  підпослідовність, що дає той самий стан. */
  #maybeCompact() {
    if (!COMPACT_AT || this.journal.length < this.nextCompactAt) return;
    const { events: kept, dropped } = compactTail(this.journal);
    this.nextCompactAt = this.journal.length + COMPACT_AT;
    if (dropped / this.journal.length < COMPACT_MIN_GAIN) return;

    const before = this.journal.length;
    try {
      // Спершу архів: втратити холодну історію гірше, ніж записати в неї двічі.
      // Повторний діапазон після падіння між кроками читач розрізнить за gseq.
      const fresh = this.journal.filter((ev) => ev.gseq > this.archivedThrough);
      if (fresh.length) {
        appendFileSync(this.archivePath, fresh.map((ev) => JSON.stringify(ev)).join('\n') + '\n');
      }

      this.journal = kept;
      this.seen = new Map(kept.map((ev) => [`${ev.author}:${ev.lseq}`, ev]));
      this.archivedThrough = this.head.gseq;
      this.compacted = true;
      this.nextCompactAt = kept.length + COMPACT_AT;
      if (!this.#writeCheckpoint()) throw new Error(this.checkpointError);

      const tmp = `${this.path}.${process.pid}.tmp`;
      writeFileSync(tmp, kept.map((ev) => JSON.stringify(ev)).join('\n') + '\n');
      renameSync(tmp, this.path);
      this.compactError = null;
      log(`[${this.name}] журнал стиснуто: ${before} -> ${kept.length} подій, ` +
          `історія в ${this.name}.archive.jsonl`);
    } catch (error) {
      // Журнал у памʼяті вже стиснутий, на диску -- ще ні. Обидва дають той
      // самий стан, а наступний commit ляже в кінець файлу як завжди.
      this.compactError = error.message;
      log(`[${this.name}] стиснення не завершилось: ${error.message}`);
    }
  }

  status() {
    return {
      session: this.name,
      head: this.head.gseq,
      hash: this.head.hash,
      online: this.clients.size,
      peers: this.peers(),
      clients: this.onlineClients(),
      authors: this.authorStats(),
      locks: this.lockList(),
      presence: this.presenceList(),
      journal_events: this.journal.length,
      compacted: this.compacted,
      archived_through: this.archivedThrough,
      served_events: this.servedEvents,
      dropped_events: this.droppedEvents,
      journal_error: this.loadError,
      compact_error: this.compactError,
      checkpoint_error: this.checkpointError,
    };
  }

  /** Адресна доставка: файлові чанки не потрібні всій кімнаті. */
  sendTo(author, msg) {
    const raw = JSON.stringify(msg);
    let delivered = 0;
    for (const c of this.clients) {
      if (c.author !== author) continue;
      if (deliver(c, raw)) delivered += 1;
    }
    return delivered;
  }

  broadcast(msg, except = null) {
    const raw = JSON.stringify(msg);
    for (const c of this.clients) {
      if (c === except) continue;
      deliver(c, raw);
    }
  }
}

/** Журнали на диску: сесія могла бути вивантажена з памʼяті, а історія лишилась. */
function journalsOnDisk() {
  try {
    return readdirSync(JOURNAL_DIR)
      .filter((name) => name.endsWith('.jsonl'))
      .map((name) => {
        const stat = statSync(join(JOURNAL_DIR, name));
        return {
          session: name.slice(0, -'.jsonl'.length),
          bytes: stat.size,
          updated_at: stat.mtimeMs / 1000,
          in_memory: sessions.has(name.slice(0, -'.jsonl'.length)),
        };
      });
  } catch (error) {
    log(`не читається ${JOURNAL_DIR}: ${error.message}`);
    return [];
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

/** Відправка з контролем буфера: хто не читає, той більше не учасник. */
/** Назви в присутності -- лише для показу, тож обрізаємо їх на вході. */
function trimNames(view) {
  if (view === null || typeof view !== 'object') return null;
  const names = {};
  for (const [key, value] of Object.entries(view.names || {})) {
    if (typeof value === 'string') names[key] = value.slice(0, PRESENCE_MAX_LABEL);
  }
  return { ...view, names };
}

function deliver(client, raw) {
  const { ws } = client;
  if (ws.readyState !== 1) return false;
  if (ws.bufferedAmount > MAX_BUFFERED) {
    log(`${client.author || '(без join)'} не встигає читати: ${ws.bufferedAmount} Б у буфері — розриваю`);
    ws.terminate();
    return false;
  }
  ws.send(raw);
  return true;
}

/** /health показує IP, авторів і статистику -- за наявності токена він теж
 *  під токеном. Приймається і заголовок, і ?token= для браузера й M4L. */
function healthAllowed(req) {
  if (!TOKEN) return true;
  const header = req.headers['x-relay-token'];
  if (typeof header === 'string' && tokenOk(header)) return true;
  try {
    return tokenOk(new URL(req.url, 'http://relay').searchParams.get('token') ?? '');
  } catch {
    return false;
  }
}

const http = createServer((req, res) => {
  if (req.url === '/health' || req.url.startsWith('/health?')) {
    if (!healthAllowed(req)) {
      res.writeHead(401, { 'content-type': 'application/json', 'access-control-allow-origin': '*' });
      res.end(JSON.stringify({ ok: false, error: 'потрібен токен relay' }));
      return;
    }
    const body = [...sessions.values()].map((s) => s.status());
    res.writeHead(200, {
      'content-type': 'application/json',
      'access-control-allow-origin': '*',
    });
    res.end(JSON.stringify({
      ok: true,
      proto: PROTO,
      now: Date.now() / 1000,
      sessions: body,
      journals: journalsOnDisk(),
    }, null, 2));
    return;
  }
  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      'access-control-allow-origin': '*',
      'access-control-allow-methods': 'GET, OPTIONS',
      'access-control-allow-headers': 'content-type',
    }).end();
    return;
  }
  res.writeHead(404).end();
});

const wss = new WebSocketServer({ server: http, maxPayload: MAX_WS_PAYLOAD });

// Усі з'єднання, включно з тими, що ще не зробили join: половинчасто відкритий
// сокет без join теж треба прибирати, інакше він висітиме вічно.
const clients = new Set();

const heartbeat = setInterval(() => {
  const now = Date.now() / 1000;
  for (const client of clients) {
    const idle = now - client.lastSeen;
    if (idle > STALE_SEC) {
      const who = client.author || '(без join)';
      log(`${who} мовчить ${Math.round(idle)} с — розриваю зʼєднання`);
      client.ws.terminate(); // 'close' прибере його з сесії й розішле peers
      continue;
    }
    if (client.ws.readyState === 1) client.ws.ping();
  }
}, HEARTBEAT_SEC * 1000);

// Окремий цикл від heartbeat: TTL лока міряється хвилинами, а не секундами,
// і чистити його треба навіть у кімнаті, де всі мовчать. Тут же вивантажуються
// сесії, які давно спорожніли.
const sweep = setInterval(() => {
  const now = Date.now() / 1000;
  for (const [name, session] of sessions) {
    if (session.expireLocks(now)) session.broadcast({ m: 'locks', locks: session.lockList() });
    if (session.clients.size) {
      session.emptySince = null;
      continue;
    }
    if (session.emptySince === null) {
      session.emptySince = now;
      continue;
    }
    if (now - session.emptySince < SESSION_IDLE_SEC) continue;
    sessions.delete(name);
    log(`[${name}] порожня ${Math.round(now - session.emptySince)} с — вивантажую з памʼяті`);
  }
}, Math.max(1, Math.min(HEARTBEAT_SEC, 15)) * 1000);

wss.on('connection', (ws, req) => {
  const forwarded = String(req.headers['x-forwarded-for'] || '').split(',')[0].trim();
  const socket = req.socket || {};
  const ip = forwarded || socket.remoteAddress || '';
  const client = {
    ws,
    author: null,
    session: null,
    ip: ip.replace(/^::ffff:/, ''),
    port: socket.remotePort ?? null,
    connectedAt: Date.now() / 1000,
    lastSeen: Date.now() / 1000,
    lastPresenceAt: 0,
    budget: SUBMIT_BURST,
    refilledAt: Date.now() / 1000,
    limitedEvents: 0,
    limitedLoggedAt: 0,
    info: null,
  };
  clients.add(client);

  // Будь-який трафік від клієнта -- ознака життя: і app-level ping daemon'а
  // (кожні 15 с, clock sync), і автоматичний pong на наш ws-ping.
  ws.on('pong', () => { client.lastSeen = Date.now() / 1000; });

  const send = (msg) => {
    if (ws.readyState === 1) ws.send(JSON.stringify(msg));
  };

  ws.on('message', (raw) => {
    const t1 = Date.now() / 1000;
    client.lastSeen = t1;
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
        if (!tokenOk(msg.token)) {
          send({ m: 'error', code: 'bad_token', text: 'потрібен токен relay' });
          log(`join відхилено: невірний токен з ${client.ip}`);
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
          locks: session.lockList(),
          presence: session.presenceList(),
        });
        const since = Number(msg.since) || 0;
        const tail = session.tailSince(since);
        const served = COMPACT_JOIN ? compactTail(tail) : { events: tail, dropped: 0 };
        session.servedEvents += served.events.length;
        session.droppedEvents += served.dropped;
        for (const ev of served.events) send({ m: 'commit', event: ev });
        session.broadcast({ m: 'peers', peers: session.peers() });
        const tailText = served.dropped
          ? `хвіст ${tail.length} -> ${served.events.length}`
          : `хвіст ${tail.length}`;
        log(`[${session.name}] + ${client.author} (${session.clients.size} онлайн, since=${since}, ${tailText})`);
        break;
      }

      case 'submit': {
        if (!client.session) return send({ m: 'error', code: 'not_joined', text: 'спершу join' });
        if (!allowSubmit(client, t1)) {
          client.limitedEvents += 1;
          // Один рядок на сплеск, а не на кожну подію: інакше лог сам стане
          // другим потоком навантаження.
          if (t1 - client.limitedLoggedAt > 5) {
            client.limitedLoggedAt = t1;
            log(`[${client.session.name}] ${client.author} перевищив ${SUBMIT_RATE} подій/с ` +
                `(відхилено ${client.limitedEvents})`);
          }
          // Подія не втрачається: у daemon вона лишається в outbox до commit.
          return send({ m: 'error', code: 'rate_limited', text: `не більше ${SUBMIT_RATE} подій/с` });
        }
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
        if (result.acked) {
          // Подія вже в журналі, але після стиснення її там могло не лишитись:
          // повернути її самою собою не вийде, тож просто підтверджуємо lseq.
          send({ m: 'ack', lseq: e.lseq });
          break;
        }
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
      case 'file_chunk':
      // Знімок стану ходить тією самою трубою, що й файли: relay його не
      // журналює й не розуміє -- це не подія, а вирівнювання між учасниками.
      case 'peer_state_request':
      case 'peer_state_error':
      case 'peer_state_chunk': {
        if (!client.session) return send({ m: 'error', code: 'not_joined', text: 'спершу join' });
        const relayed = { ...msg, from: client.author };
        // to -- адресат: чанк потрібен тому, хто просив, а не всій кімнаті.
        // Без to (старий клієнт) лишається broadcast, як було.
        if (typeof msg.to === 'string' && msg.to) client.session.sendTo(msg.to, relayed);
        else client.session.broadcast(relayed, client);
        break;
      }

      // Розсинхрон версій (vision.md §8) виглядає як "sync не працює": подія
      // доходить, а приймальний бік про такий тип не знає і мовчки її ковтає.
      // Тут це стає видимим одразу при конекті, а не після звірки двох логів.
      case 'client_info': {
        if (!client.session) return send({ m: 'error', code: 'not_joined', text: 'спершу join' });
        client.info = {
          live: msg.live,
          script: msg.script,
          features: msg.features || [],
          events: msg.events || [],
        };
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

      // Локи не журналяться і не впливають на порядок подій -- relay тут
      // арбітр, а не секвенсор. Обʼєкт для relay -- непрозорий рядок: що саме
      // ним адресовано, знає тільки клієнт (vision.md §4).
      case 'lock':
      case 'unlock': {
        if (!client.session) return send({ m: 'error', code: 'not_joined', text: 'спершу join' });
        const object = String(msg.object ?? '');
        if (!validAuthor(object)) {
          return send({ m: 'error', code: 'bad_lock', text: 'object має містити 1–128 друкованих символів' });
        }
        const session = client.session;
        if (msg.m === 'unlock') {
          if (session.releaseLock(client.author, object)) {
            session.broadcast({ m: 'locks', locks: session.lockList() });
            log(`[${session.name}] лок ${object} звільнив ${client.author}`);
          }
          break;
        }
        if (!session.locks.has(object) && session.lockCountOf(client.author) >= LOCKS_PER_AUTHOR) {
          return send({ m: 'error', code: 'too_many_locks', text: `максимум ${LOCKS_PER_AUTHOR} локів на гравця` });
        }
        const asked = Number(msg.ttl);
        const ttl = Math.min(asked > 0 ? asked : LOCK_TTL_SEC, LOCK_TTL_MAX);
        const taken = session.acquireLock(client.author, object, msg.label, ttl);
        if (!taken.ok) {
          send({ m: 'lock_denied', object, author: taken.lock.author, label: taken.lock.label });
          log(`[${session.name}] ${client.author} не отримав лок ${object}: тримає ${taken.lock.author}`);
          break;
        }
        session.broadcast({ m: 'locks', locks: session.lockList() });
        break;
      }

      // Присутність не журналюється і не має порядку: це поточний факт про
      // гравця, а не подія. Тому стан на сесії, а не труба -- інакше M4L
      // Status (він читає лише /health) не побачив би нічого, а пізній
      // учасник чекав би, поки партнер поворухнеться.
      case 'presence': {
        if (!client.session) return send({ m: 'error', code: 'not_joined', text: 'спершу join' });
        // Зламаний клієнт гаситься мовчки: відповідь помилкою повернула б
        // йому шторм назад.
        if (t1 - client.lastPresenceAt < 1 / PRESENCE_RATE) break;
        client.lastPresenceAt = t1;
        const view = msg.view === null || typeof msg.view === 'object' ? msg.view : undefined;
        if (view === undefined) {
          return send({ m: 'error', code: 'bad_presence', text: 'view має бути обʼєктом або null' });
        }
        if (view === null) client.session.clearPresence(client.author);
        else client.session.setPresence(client.author, view, msg.following);
        client.session.broadcast({ m: 'presence', list: client.session.presenceList() });
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
    clients.delete(client);
    if (!client.session) return;
    client.session.clients.delete(client);
    // Гравець зник -- його локи зникають разом із ним, не чекаючи TTL. Саме
    // тому це працює швидко: обрив мережі тепер помічає heartbeat, а не
    // системний таймаут TCP.
    const stillHere = [...client.session.clients].some((c) => c.author === client.author);
    if (!stillHere && client.session.releaseLocksOf(client.author)) {
      client.session.broadcast({ m: 'locks', locks: client.session.lockList() });
    }
    if (!stillHere && client.session.clearPresence(client.author)) {
      client.session.broadcast({ m: 'presence', list: client.session.presenceList() });
    }
    client.session.broadcast({ m: 'peers', peers: client.session.peers() });
    log(`[${client.session.name}] - ${client.author} (${client.session.clients.size} онлайн)`);
  });

  ws.on('error', (e) => log('ws error:', e.message));
});

http.listen(PORT, () => {
  log(`relay слухає ws://0.0.0.0:${PORT} (proto ${PROTO}), журнали в ${JOURNAL_DIR}`);
});

// Checkpoint пишеться після кожного commit, тож тут лишається тільки чесно
// попрощатися: клієнт має побачити close, а не обрив, і одразу піти в реконект.
let shuttingDown = false;
function shutdown(signal) {
  if (shuttingDown) return;
  shuttingDown = true;
  log(`${signal}: закриваю relay, head сесій уже в checkpoint`);
  clearInterval(heartbeat);
  clearInterval(sweep);
  for (const client of clients) {
    try { client.ws.close(1001, 'relay зупиняється'); } catch {}
  }
  http.close(() => {
    log('relay зупинено');
    process.exit(0);
  });
  setTimeout(() => {
    log('клієнти не відпустили зʼєднання — вихід примусовий');
    process.exit(0);
  }, 3000).unref();
}
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));
