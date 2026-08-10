// File Sync Layer -- незалежний від event log.
//
// vision.md §4: цей шар нічого не знає про журнал, а журнал -- про нього.
// Relay ці повідомлення НЕ комітить, а просто пересилає партнерам.
//
// Що синхронізується автоматично: семпли, ресемпли, freeze -- дані адитивні
// й на практиці незмінні, тож конфлікту немає за побудовою.
// Що НЕ синхронізується: .als (джерело правди для структури -- журнал, а не файл)
// і регенеровані Live файли (.asd, Backup/).

import { createHash } from 'node:crypto';
import {
  existsSync, lstatSync, mkdirSync, readdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync,
} from 'node:fs';
import { dirname, isAbsolute, join, posix, relative, resolve, sep, win32 } from 'node:path';

const CHUNK = 192 * 1024; // байтів сирих даних на повідомлення
const SCAN_DEBOUNCE_MS = 1500;
const MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024;
const HASH_RE = /^[0-9a-f]{16}$/;

/** Тека всередині проєкту -> чи синхронізуємо. */
const SYNC_DIRS = ['Samples'];
const SKIP_DIRS = new Set(['Backup', 'Ableton Project Info']);
const SKIP_EXT = new Set(['.asd', '.als', '.alp', '.tmp']);

function ext(name) {
  const i = name.lastIndexOf('.');
  return i < 0 ? '' : name.slice(i).toLowerCase();
}

/** Канонічний шлях протоколу: тільки файл усередині дозволеної теки проєкту. */
function safeRelPath(path) {
  if (typeof path !== 'string' || !path || path.length > 4096 || path.includes('\0')) return null;
  if (path.includes('\\') || isAbsolute(path) || posix.isAbsolute(path) || win32.isAbsolute(path)) return null;
  const parts = path.split('/');
  if (parts.some((p) => !p || p === '.' || p === '..')) return null;
  if (!SYNC_DIRS.includes(parts[0]) || parts.length < 2) return null;
  return parts.join('/');
}

function fullInside(root, relPath) {
  const rootFull = resolve(root);
  const parts = relPath.split('/');
  const full = resolve(rootFull, ...parts);
  const back = relative(rootFull, full);
  if (!back || back.startsWith(`..${sep}`) || back === '..' || isAbsolute(back)) return null;
  // Junction/symlink у проміжній теці не має права перенаправити запис назовні.
  let cursor = rootFull;
  for (const part of parts.slice(0, -1)) {
    cursor = join(cursor, part);
    try {
      if (existsSync(cursor) && lstatSync(cursor).isSymbolicLink()) return null;
    } catch {
      return null;
    }
  }
  return full;
}

function digest(data) {
  return createHash('sha256').update(data).digest('hex').slice(0, 16);
}

export class FileSync {
  /**
   * @param {object} opts
   * @param {(msg:object)=>void} opts.send  надіслати повідомлення в relay
   * @param {(...a:any)=>void}   opts.log
   */
  constructor({ send, log }) {
    this.send = send;
    this.log = log;
    this.root = null;
    this.manifest = new Map(); // relPath -> {size, hash, mtimeMs}
    this.incoming = new Map(); // relPath -> {total, parts:[], bytes, expected}
    this.wanted = new Map();   // relPath -> {size, hash}
    this._timer = null;
  }

  /** Теку проєкту виводимо зі шляху .als, який повідомляє bridge. */
  setProjectFile(filePath) {
    if (filePath) this.setProjectRoot(dirname(filePath));
  }

  setProjectRoot(root) {
    if (!root || root === this.root) return;
    this.root = root;
    this.log(`filesync: тека проєкту ${root}`);
    this.rescan();
  }

  scheduleRescan() {
    if (this._timer) clearTimeout(this._timer);
    this._timer = setTimeout(() => this.rescan(), SCAN_DEBOUNCE_MS);
    this._timer.unref?.();
  }

  rescan() {
    if (!this.root || !existsSync(this.root)) return;
    const found = new Map();
    for (const dir of SYNC_DIRS) {
      const base = join(this.root, dir);
      if (existsSync(base)) this.#walk(base, found);
    }
    let changed = 0;
    for (const [rel, info] of found) {
      const prev = this.manifest.get(rel);
      if (!prev || prev.hash !== info.hash) changed += 1;
    }
    const removed = [...this.manifest.keys()].filter((k) => !found.has(k));
    this.manifest = found;
    if (changed || removed.length) {
      this.log(`filesync: ${found.size} файлів (${changed} нових/змінених)`);
    }
    this.announce();
  }

  #walk(dir, out) {
    let entries;
    try {
      entries = readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const e of entries) {
      if (e.isSymbolicLink()) continue;
      const full = join(dir, e.name);
      if (e.isDirectory()) {
        if (!SKIP_DIRS.has(e.name)) this.#walk(full, out);
        continue;
      }
      if (SKIP_EXT.has(ext(e.name))) continue;
      let st;
      try {
        st = statSync(full);
      } catch {
        continue;
      }
      const rel = relative(this.root, full).split(sep).join('/');
      const prev = this.manifest.get(rel);
      // Розмір сам по собі не доводить незмінність: ресемпл може бути переписаний
      // байт у байт тієї ж довжини. mtime лишає швидкий шлях для незмінених файлів.
      const hash = prev && prev.size === st.size && prev.mtimeMs === st.mtimeMs
        ? prev.hash
        : this.#hash(full);
      if (hash) out.set(rel, { size: st.size, hash, mtimeMs: st.mtimeMs });
    }
  }

  #hash(full) {
    try {
      return digest(readFileSync(full));
    } catch {
      return null;
    }
  }

  announce() {
    if (!this.root) return;
    this.send({
      m: 'files_manifest',
      files: [...this.manifest].map(([path, i]) => ({ path, size: i.size, hash: i.hash })),
    });
  }

  /** Партнер розповів, що має. Просимо те, чого бракує. */
  onManifest(files) {
    if (!this.root) return;
    const missing = [];
    for (const f of files || []) {
      const path = safeRelPath(f?.path);
      if (!path || !Number.isSafeInteger(f.size) || f.size < 0 || f.size > MAX_FILE_BYTES || !HASH_RE.test(f.hash)) {
        this.log(`filesync: відхилено некоректний запис маніфесту ${String(f?.path)}`);
        continue;
      }
      const mine = this.manifest.get(path);
      if (!mine || mine.hash !== f.hash) {
        if (!this.wanted.has(path)) missing.push({ path, size: f.size, hash: f.hash });
      }
    }
    if (!missing.length) return;
    this.log(`filesync: бракує ${missing.length} файлів, запитую`);
    for (const expected of missing) {
      this.wanted.set(expected.path, expected);
      this.send({ m: 'file_request', path: expected.path });
    }
  }

  /** Партнер попросив файл -- шлемо чанками. */
  onRequest(path) {
    path = safeRelPath(path);
    if (!this.root || !path || !this.manifest.has(path)) return;
    const full = fullInside(this.root, path);
    if (!full) return;
    let data;
    try {
      data = readFileSync(full);
    } catch (e) {
      return this.log(`filesync: не читається ${path}: ${e.message}`);
    }
    const advertised = this.manifest.get(path);
    if (data.length !== advertised.size || digest(data) !== advertised.hash) {
      this.log(`filesync: ${path} змінився після маніфесту, відкладаю передачу до рескану`);
      this.scheduleRescan();
      return;
    }
    const total = Math.max(1, Math.ceil(data.length / CHUNK));
    for (let i = 0; i < total; i++) {
      this.send({
        m: 'file_chunk',
        path,
        seq: i,
        total,
        data: data.subarray(i * CHUNK, (i + 1) * CHUNK).toString('base64'),
      });
    }
    this.log(`filesync: віддав ${path} (${data.length} Б, ${total} чанків)`);
  }

  onChunk({ path, seq, total, data }) {
    path = safeRelPath(path);
    const expected = path ? this.wanted.get(path) : null;
    if (!this.root || !path || !expected) return;
    const expectedTotal = Math.max(1, Math.ceil(expected.size / CHUNK));
    if (!Number.isSafeInteger(total) || total !== expectedTotal ||
        !Number.isSafeInteger(seq) || seq < 0 || seq >= total || typeof data !== 'string') {
      return this.#rejectIncoming(path, 'некоректна нумерація чанків');
    }
    let acc = this.incoming.get(path);
    if (!acc) {
      acc = { total, parts: new Array(total).fill(null), bytes: 0, expected };
      this.incoming.set(path, acc);
    }
    if (acc.parts[seq]) return;
    let buf;
    try {
      buf = Buffer.from(data, 'base64');
    } catch {
      return this.#rejectIncoming(path, 'чанк не є base64');
    }
    if (buf.length > CHUNK || acc.bytes + buf.length > expected.size) {
      return this.#rejectIncoming(path, 'розмір чанків перевищує маніфест');
    }
    acc.parts[seq] = buf;
    acc.bytes += buf.length;
    if (acc.parts.some((p) => p === null)) return;

    const complete = Buffer.concat(acc.parts);
    if (complete.length !== expected.size || digest(complete) !== expected.hash) {
      return this.#rejectIncoming(path, 'hash або розмір не збігається з маніфестом');
    }

    const full = fullInside(this.root, path);
    if (!full) return this.#rejectIncoming(path, 'шлях вийшов за теку проєкту');
    const tmp = `${full}.abletonmp-${process.pid}-${Date.now()}.tmp`;
    try {
      mkdirSync(dirname(full), { recursive: true });
      writeFileSync(tmp, complete);
      renameSync(tmp, full);
      this.log(`filesync: отримав ${path} (${acc.bytes} Б)`);
    } catch (e) {
      try { rmSync(tmp, { force: true }); } catch {}
      this.log(`filesync: не записався ${path}: ${e.message}`);
    }
    this.incoming.delete(path);
    this.wanted.delete(path);
    this.scheduleRescan();
  }

  #rejectIncoming(path, reason) {
    this.incoming.delete(path);
    this.wanted.delete(path);
    this.log(`filesync: відхилено ${path}: ${reason}`);
  }
}
