// Присутність: хто на що дивиться, і режим follow.
//
// Погляд не змінює проєкт, тож у журнал він не йде -- це ефемерний стан сесії,
// як локи. Але на відміну від лока присутність не має TTL: вона істинна, поки
// людина мовчки дивиться на трек.
//
// Тут живе вся логіка, яку можна перевірити без мережі: підпис виду, тротлінг
// і правила, за якими follow дозволений або ні.

/** Канонічний підпис виду. Назви в нього не входять навмисно: перейменування
 *  треку не є рухом погляду, і партнеру нема чого про нього дізнаватись
 *  окремим повідомленням -- назва оновиться при наступному русі. */
export function viewSignature(view) {
  if (!view || typeof view !== 'object') return 'none';
  const track = view.track?.id ? `${view.track.kind || 'track'}:${view.track.id}` : '-';
  const scene = view.scene?.id || '-';
  const clip = view.clip ? `${view.clip.track}:${view.clip.scene}` : '-';
  return `${track}|${scene}|${clip}|${view.screen || '-'}`;
}

const nameOf = (view, key, fallback) => view?.names?.[key] || fallback;

/** Людський рядок про чужі погляди. null -- нікого немає. */
export function describePresence(list, me) {
  const others = (list || []).filter((entry) => entry.author !== me && entry.view);
  if (!others.length) return null;
  return others.map((entry) => {
    const view = entry.view;
    const where = [
      nameOf(view, 'track', view.track?.id),
      nameOf(view, 'scene', view.scene?.id),
    ].filter(Boolean).join(' / ') || 'ніде';
    const tail = entry.following ? ` (слідує за ${entry.following})` : '';
    return `${entry.author}: ${where}${tail}`;
  }).join('; ');
}

/**
 * Чи можна зараз піти за партнером. Причина відмови -- текстом, бо вона
 * завжди має бути видимою: мовчазний follow, що не працює, гірший за його
 * відсутність.
 */
export function shouldFollow({ list, me, target, pausedUntil = 0, now = Date.now() }) {
  if (!target) return { ok: false };
  const entry = (list || []).find((item) => item.author === target);
  if (!entry) return { ok: false, reason: `${target} не в сесії` };
  if (!entry.view) return { ok: false, reason: `${target} нікуди не дивиться` };
  // Взаємний follow -- це двоє, що возять вид один одному по колу.
  if (entry.following === me) {
    return { ok: false, reason: `${target} уже слідує за тобою, взаємний follow вимкнено` };
  }
  if (now < pausedUntil) {
    return { ok: false, reason: null }; // пауза після власної дії, мовчки
  }
  return { ok: true, view: entry.view };
}

export class PresenceKeeper {
  constructor({ send, log, minIntervalMs = 250 }) {
    this.send = send;
    this.log = log;
    this.minIntervalMs = minIntervalMs;
    this.enabled = false;      // вмикається, лише коли relay показав, що вміє
    this.following = null;
    this.peers = [];
    this.lastSignature = null;
    this.lastSentAt = 0;
    this.pending = null;
    this.timer = null;
    this.lastLine = null;
  }

  /** Relay уміє присутність (у welcome прийшов список) -- можна починати. */
  enable() {
    this.enabled = true;
  }

  /** Старий relay відповів unknown_msg: більше не пробуємо. */
  disable() {
    this.enabled = false;
    this.#stopTimer();
    this.pending = null;
  }

  /** Новий вид від bridge. */
  update(view, now = Date.now()) {
    if (!this.enabled) return false;
    const signature = viewSignature(view);
    if (signature === this.lastSignature) return false;
    // Я щойно повторив вид того, за ким слідую -- розповідати про це назад
    // означало б відбити його ж рух йому ж.
    if (this.following) {
      const leader = this.peers.find((entry) => entry.author === this.following);
      if (leader && viewSignature(leader.view) === signature) {
        this.lastSignature = signature;
        return false;
      }
    }
    this.pending = view;
    return this.#flush(now);
  }

  /** Bridge замовк: присутність має зникнути, а не застигнути. */
  clear(now = Date.now()) {
    if (!this.enabled || this.lastSignature === 'none') return false;
    this.pending = null;
    this.#stopTimer();
    this.lastSignature = 'none';
    this.lastSentAt = now;
    this.send({ m: 'presence', view: null });
    return true;
  }

  setFollowing(author, now = Date.now()) {
    if (this.following === author) return;
    this.following = author || null;
    // Партнери мають одразу бачити, хто за ким іде: саме на це поле спирається
    // заборона взаємного follow.
    if (!this.enabled || this.lastSignature === null) return;
    this.lastSentAt = now;
    this.send({ m: 'presence', view: this.pendingView(), following: this.following });
  }

  pendingView() {
    return this.pending ?? this.lastView ?? null;
  }

  /** Повний список кімнати від relay. Логуємо лише зміну. */
  onPresence(list, me) {
    this.peers = Array.isArray(list) ? list : [];
    const line = describePresence(this.peers, me);
    if (line === this.lastLine) return;
    this.lastLine = line;
    this.log(line ? `дивляться: ${line}` : 'усі дивляться кудись інде');
  }

  /** Розрив звʼязку: relay нас забув, тож наступний вид треба слати заново. */
  reset() {
    this.#stopTimer();
    this.pending = null;
    this.lastSignature = null;
    this.lastLine = null;
    this.peers = [];
  }

  #flush(now) {
    const wait = this.minIntervalMs - (now - this.lastSentAt);
    if (wait > 0) {
      // Trailing, а не leading: партнер має отримати ОСТАННІЙ вид, інакше
      // застрягне на випадковому кадрі з середини руху.
      if (!this.timer) {
        this.timer = setTimeout(() => {
          this.timer = null;
          if (this.pending !== null) this.#flush(Date.now());
        }, wait);
        if (this.timer.unref) this.timer.unref();
      }
      return false;
    }
    const view = this.pending;
    this.pending = null;
    this.lastView = view;
    this.lastSignature = viewSignature(view);
    this.lastSentAt = now;
    this.send({ m: 'presence', view, following: this.following });
    return true;
  }

  #stopTimer() {
    if (!this.timer) return;
    clearTimeout(this.timer);
    this.timer = null;
  }
}
