// Збірка повного стану, що приходить від bridge чанками.
//
// UDP не гарантує ні доставки, ні порядку, тож зібраний наполовину стан має
// лишатись невидимим: назовні віддається тільки повний знімок. Частковий
// протухає за таймаутом, і його місце займає наступний запит.

import { createHash } from 'node:crypto';

const MAX_CHUNKS = 4096;

function canonical(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  return '{' + Object.keys(value).sort()
    .map((k) => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
}

/**
 * Відбиток вмісту знімка. Час зняття, версії Live і скрипта з нього виключені
 * навмисно: digest існує саме для того, щоб порівняти два сети між машинами,
 * а вони ніколи не збігаються в часі й не зобовʼязані збігатись у версіях.
 * Ключі сортуються, бо JSON із Python і з JS має різний порядок полів.
 *
 * `playing` виключений із тієї самої причини, тільки гострішої: спільного
 * плейхеда ми свідомо не робимо (docs/COVERAGE.md, тир 4), кожен грає своє --
 * тож із ним у відбитку рядок «стан збігається повністю» був недосяжним
 * за побудовою, і digest перетворювався на шум.
 */
export function stateDigest(state) {
  const {
    at: _at, script: _script, live: _live, playing: _playing, ...content
  } = state || {};
  return createHash('sha256').update(canonical(content)).digest('hex').slice(0, 16);
}

/** Короткий підсумок для лога: скільки чого вміщує знімок. */
export function summarize(state) {
  const tracks = state?.tracks || [];
  const aux = state?.aux_tracks || [];
  let devices = 0;
  let parameters = 0;
  let clips = 0;
  let notes = 0;
  for (const track of [...tracks, ...aux]) {
    for (const entry of track.devices || []) {
      devices += 1;
      parameters += (entry.parameters || []).length;
    }
    for (const clip of track.clips || []) {
      clips += 1;
      notes += (clip.notes || []).length;
    }
  }
  return {
    tracks: tracks.length,
    aux_tracks: aux.length,
    scenes: (state?.scenes || []).length,
    devices,
    parameters,
    clips,
    notes,
  };
}

export class StateCollector {
  constructor({ log, onComplete, ttlMs = 15000 }) {
    this.log = log;
    this.onComplete = onComplete;
    this.ttlMs = ttlMs;
    this.current = null;
  }

  /** Повертає 'complete' | 'partial' | 'ignored' | 'broken'. */
  chunk(msg) {
    const { id, seq, total } = msg;
    if (!Number.isSafeInteger(total) || total < 1 || total > MAX_CHUNKS) return 'ignored';
    if (!Number.isSafeInteger(seq) || seq < 0 || seq >= total) return 'ignored';
    if (typeof msg.data !== 'string') return 'ignored';

    const now = Date.now();
    if (this.current && this.current.id !== id) {
      // Новий знімок скасовує старий: доганяти напівзібраний немає сенсу,
      // свіжий однаково повніший.
      const missing = this.current.total - this.current.parts.size;
      if (missing > 0) this.log(`state: знімок ${this.current.id} лишився без ${missing} чанків`);
      this.current = null;
    }
    if (this.current && now - this.current.startedAt > this.ttlMs) {
      this.log(`state: знімок ${this.current.id} протух, починаю заново`);
      this.current = null;
    }
    if (!this.current) {
      // Збірку можна почати з будь-якого чанка: перший міг загубитись, а
      // повнота однаково перевіряється за набором seq.
      this.current = { id, total, parts: new Map(), startedAt: now, chars: msg.chars ?? null };
    }
    if (this.current.total !== total) return 'ignored';

    this.current.parts.set(seq, msg.data);
    if (this.current.parts.size < total) return 'partial';

    const blob = Array.from({ length: total }, (_, i) => this.current.parts.get(i)).join('');
    const { id: doneId, chars } = this.current;
    this.current = null;
    if (Number.isSafeInteger(chars) && blob.length !== chars) {
      this.log(`state: знімок ${doneId} зібрався у ${blob.length} символів замість ${chars}`);
      return 'broken';
    }
    let state;
    try {
      state = JSON.parse(blob);
    } catch (error) {
      this.log(`state: знімок ${doneId} не парситься: ${error.message}`);
      return 'broken';
    }
    this.onComplete(state, { id: doneId, chars: blob.length });
    return 'complete';
  }

  pending() {
    if (!this.current) return null;
    return { id: this.current.id, got: this.current.parts.size, total: this.current.total };
  }

  reset() {
    this.current = null;
  }
}
