// Стиснення хвоста журналу перед відправкою клієнту.
//
// Журнал на диску лишається недоторканим: повна історія -- холодний архів
// для undo й аудиту (vision.md §5 п.8). Стискається тільки те, що летить
// у клієнта на join. Із серії подій, де кожна наступна повністю перекриває
// попередню, доїжджає остання: стан після застосування той самий, але Live
// не проганяє тисячу проміжних положень фейдера і не перезапускає кліпи,
// зупинені пів години тому.
//
// Правило згортання одне -- "останній перемагає в межах ключа". Ключ описує
// адресу, яку подія перезаписує цілком. Якщо адресу неможливо виразити
// (структурні події) або тип невідомий, подія не згортається ніколи: старий
// relay не має права мовчки викидати подію, яку навчився слати новий bridge.

const BARRIER = new Set(['SceneLaunch', 'StopAllClips']);
const PLAYBACK = new Set(['ClipLaunch', 'ClipStop', 'SceneLaunch', 'StopAllClips']);

function canonical(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  return '{' + Object.keys(value).sort()
    .map((k) => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
}

// Return і Master живуть в окремому просторі ідентичності, тож kind -- частина
// адреси, а не косметика: aux-подія не сміє згорнутись у подію звичайного треку.
function trackKey(track) {
  if (!track || typeof track !== 'object') return '?';
  return `${track.kind || 'track'}:${track.id ?? '?'}`;
}

/** Адреса, яку подія перезаписує цілком. null -- подію не згортаємо. */
export function supersedeKey(ev) {
  const p = ev.payload || {};
  switch (ev.type) {
    case 'TempoSet':
      return 'tempo';
    case 'TransportSet':
      return 'transport';
    case 'MixerSet':
      return `mixer:${trackKey(p.track)}:${p.param}:${p.index ?? ''}`;
    case 'TrackToggle':
      return `toggle:${trackKey(p.track)}:${p.param}`;
    case 'ObjectMetaSet':
      return `meta:${p.object}:${trackKey(p.track)}:${p.scene?.id ?? ''}:${p.prop}`;
    case 'DeviceParamSet':
      return `device:${trackKey(p.track)}` +
        `:${(p.chain_path || []).map((c) => c.id).join('/')}` +
        `:${p.device?.class_name}/${p.device?.class_display_name}#${p.device?.ordinal}` +
        `:${p.parameter?.name}#${p.parameter?.ordinal}`;
    case 'ClipNotesSet':
      // Регіон -- частина ключа: подія замінює ноти лише всередині нього,
      // тож два різні регіони одного кліпу не перекривають одне одного.
      return `notes:${p.track?.id}:${p.scene?.id}:${canonical(p.region)}`;
    default:
      return null;
  }
}

/**
 * Повертає підпослідовність `events` у тому ж порядку і з тими самими gseq.
 * Нові події не синтезуються: клієнт отримує рівно ті ж об'єкти, просто
 * без перекритих. Остання подія хвоста зберігається завжди, тому lastGseq
 * клієнта доходить до head.
 */
export function compactTail(events) {
  const seen = new Set();
  const kept = [];
  let barrier = false;

  for (let i = events.length - 1; i >= 0; i -= 1) {
    const ev = events[i];

    if (PLAYBACK.has(ev.type)) {
      // SceneLaunch і StopAllClips чіпають усі треки одразу, тож усе, що було
      // до них, вже неважливе. Пізніші потрекові launch/stop лишаються -- вони
      // перекривають сцену тільки на своєму треку.
      if (barrier) continue;
      if (BARRIER.has(ev.type)) {
        barrier = true;
        kept.push(ev);
        continue;
      }
      const key = `play:${ev.payload?.track?.id ?? '?'}`;
      if (seen.has(key)) continue;
      seen.add(key);
      kept.push(ev);
      continue;
    }

    const key = supersedeKey(ev);
    if (key === null) {
      kept.push(ev);
      continue;
    }
    if (seen.has(key)) continue;
    seen.add(key);
    kept.push(ev);
  }

  kept.reverse();
  return { events: kept, dropped: events.length - kept.length };
}
