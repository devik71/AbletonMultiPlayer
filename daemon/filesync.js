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
import { existsSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';

const CHUNK = 192 * 1024; // байтів сирих даних на повідомлення
const SCAN_DEBOUNCE_MS = 1500;

/** Тека всередині проєкту -> чи синхронізуємо. */
const SYNC_DIRS = ['Samples'];
const SKIP_DIRS = new Set(['Backup', 'Ableton Project Info']);
const SKIP_EXT = new Set(['.asd', '.als', '.alp', '.tmp']);

function ext(name) {
  const i = name.lastIndexOf('.');
  return i < 0 ? '' : name.slice(i).toLowerCase();
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
    this.manifest = new Map(); // relPath -> {size, hash}
    this.incoming = new Map(); // relPath -> {total, parts:[], bytes}
    this.wanted = new Set();
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
      // хеш рахуємо лише коли розмір змінився -- інакше беремо попередній
      const hash = prev && prev.size === st.size ? prev.hash : this.#hash(full);
      if (hash) out.set(rel, { size: st.size, hash });
    }
  }

  #hash(full) {
    try {
      return createHash('sha256').update(readFileSync(full)).digest('hex').slice(0, 16);
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
      const mine = this.manifest.get(f.path);
      if (!mine || mine.hash !== f.hash) {
        if (!this.wanted.has(f.path)) missing.push(f.path);
      }
    }
    if (!missing.length) return;
    this.log(`filesync: бракує ${missing.length} файлів, запитую`);
    for (const path of missing) {
      this.wanted.add(path);
      this.send({ m: 'file_request', path });
    }
  }

  /** Партнер попросив файл -- шлемо чанками. */
  onRequest(path) {
    if (!this.root || !this.manifest.has(path)) return;
    const full = join(this.root, path);
    let data;
    try {
      data = readFileSync(full);
    } catch (e) {
      return this.log(`filesync: не читається ${path}: ${e.message}`);
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
    if (!this.root) return;
    let acc = this.incoming.get(path);
    if (!acc) {
      acc = { total, parts: new Array(total).fill(null), bytes: 0 };
      this.incoming.set(path, acc);
    }
    if (acc.parts[seq]) return;
    const buf = Buffer.from(data, 'base64');
    acc.parts[seq] = buf;
    acc.bytes += buf.length;
    if (acc.parts.some((p) => p === null)) return;

    const full = join(this.root, path);
    try {
      mkdirSync(dirname(full), { recursive: true });
      writeFileSync(full, Buffer.concat(acc.parts));
      this.log(`filesync: отримав ${path} (${acc.bytes} Б)`);
    } catch (e) {
      this.log(`filesync: не записався ${path}: ${e.message}`);
    }
    this.incoming.delete(path);
    this.wanted.delete(path);
    this.scheduleRescan();
  }
}
