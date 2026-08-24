// Локи: "цей обʼєкт зараз редагують".
//
// Лок бере і знімає daemon, а не bridge: усередині Live жест виглядає як потік
// дрібних подій, і рахувати його межі -- робота для процесу, який можна
// перезапустити (vision.md §4). Початок жесту -- перша подія на обʼєкт,
// кінець -- пауза в потоці. Так пʼятихвилинне малювання automation тримає
// один лок, а не блимає ним на кожній мікрозміні.
//
// Це порада партнеру, а не заборона: relay нікого не блокує (vision.md §5 п.5),
// бо власний Live уже застосував зміну, і відкат вимагав би replay.

// Дискретні дії (launch, mute, rename) лока не потребують: вони миттєві,
// і показувати "редагує" на них -- лише блимання в чужому UI.
const CONTINUOUS = new Set([
  'TempoSet', 'MixerSet', 'DeviceParamSet', 'ClipNotesSet', 'ClipLoopSet',
  'ArrangementClipMove', 'ArrangementClipNotesSet',
  'SongPropSet',
  'SongPropSet',
]);

function trackName(registry, id, kind) {
  if (!registry) return null;
  const list = !kind || kind === 'track' ? registry.tracks : registry.aux_tracks;
  return (list || []).find((t) => t.id === id)?.name ?? null;
}

function sceneName(registry, id) {
  return (registry?.scenes || []).find((s) => s.id === id)?.name ?? null;
}

/** Обʼєкт лока + людська назва для чужого UI. null -- лок не потрібен. */
export function lockTarget(type, payload, registry) {
  if (!CONTINUOUS.has(type)) return null;
  const p = payload || {};

  if (type === 'TempoSet') return { object: 'song:tempo', label: 'темп' };

  if (type === 'SongPropSet') {
    // Своя адреса на кожну властивість: двоє можуть одночасно правити
    // розмір такту й тональність, і це не конфлікт.
    const prop = p.prop;
    if (!prop) return null;
    return { object: `song:${prop}`, label: prop };
  }

  if (type === 'SongPropSet') {
    // Своя адреса на кожну властивість: двоє можуть одночасно правити
    // розмір такту й тональність, і це не конфлікт.
    const prop = p.prop;
    if (!prop) return null;
    return { object: `song:${prop}`, label: prop };
  }

  if (type === 'ArrangementClipMove' || type === 'ArrangementClipNotesSet') {
    // Кліп в Arrangement має власний uuid -- ні сцени, ні слоту в нього немає.
    const clip = p.clip?.id;
    if (!clip) return null;
    const track = trackName(registry, p.track?.id);
    return { object: `arrclip:${clip}`, label: [track, 'Arrangement'].filter(Boolean).join(' / ') };
  }

  if (type === 'ClipNotesSet' || type === 'ClipLoopSet') {
    const track = p.track?.id;
    const scene = p.scene?.id;
    if (!track || !scene) return null;
    const names = [trackName(registry, track), sceneName(registry, scene)].filter(Boolean);
    return { object: `clip:${track}:${scene}`, label: names.join(' / ') || null };
  }

  const id = p.track?.id;
  if (!id) return null;
  // Return і Master -- окремий простір ідентичності, тож kind у ключі лока
  const kind = p.track?.kind || 'track';
  return { object: `${kind}:${id}`, label: trackName(registry, id, kind) };
}

export class LockKeeper {
  constructor({ send, log, ttlSec = 30, idleMs = 1500 }) {
    this.send = send;
    this.log = log;
    this.ttlSec = ttlSec;
    this.idleMs = idleMs;
    this.held = new Map();
    this.lastLine = null;
  }

  /** Викликається на кожну вихідну подію. Повертає взятий обʼєкт або null. */
  touch(type, payload, registry) {
    const target = lockTarget(type, payload, registry);
    if (!target) return null;
    const now = Date.now();
    let entry = this.held.get(target.object);

    if (!entry) {
      entry = { sentAt: now, timer: null };
      this.send({ m: 'lock', object: target.object, label: target.label, ttl: this.ttlSec });
    } else {
      clearTimeout(entry.timer);
      // Довгий жест не має протухнути на relay посеред руху.
      if (now - entry.sentAt > this.ttlSec * 500) {
        entry.sentAt = now;
        this.send({ m: 'lock', object: target.object, label: target.label, ttl: this.ttlSec });
      }
    }

    entry.timer = setTimeout(() => {
      this.held.delete(target.object);
      this.send({ m: 'unlock', object: target.object });
    }, this.idleMs);
    if (entry.timer.unref) entry.timer.unref();
    this.held.set(target.object, entry);
    return target;
  }

  /** Повний список локів сесії від relay -- логуємо тільки зміни. */
  onLocks(locks, me) {
    const others = (locks || []).filter((lock) => lock.author !== me);
    const line = others.map((lock) => `${lock.author} — ${lock.label || lock.object}`).join('; ');
    if (line === this.lastLine) return;
    this.lastLine = line;
    this.log(line ? `редагують: ${line}` : 'ніхто нічого не редагує');
  }

  onDenied(msg) {
    this.log(`лок ${msg.label || msg.object} тримає ${msg.author}: зміна піде, ` +
      'але останнє слово лишиться за тим, чия подія прийде пізніше');
  }

  /** Розрив звʼязку: relay зніме наші локи сам, локальні таймери зайві. */
  reset() {
    for (const entry of this.held.values()) clearTimeout(entry.timer);
    this.held.clear();
    this.lastLine = null;
  }
}
