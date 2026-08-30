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
    case 'CueSet':
    case 'CueDelete':
      // Локатор адресується часом: CuePoint.time лише на читання, а два
      // локатори не бувають на одній позиції. Тож час і є адреса, і кожна
      // з двох подій повністю визначає стан у ній -- згортання коректне
      // в обидва боки, і create+delete, і delete+create.
      return `cue:${p.time}`;
    case 'ChainMixerSet':
      // Своя адреса на пару (ланцюг, параметр): гучність пада не перекриває
      // його ж панораму, а різні пади не заважають одне одному.
      return `chain:${p.chain?.id}:${p.param}`;
    case 'ClipWarpSet':
      // Маркери описують ВІДОБРАЖЕННЯ цілком, тож набір перезаписується
      // повністю: із серії рухів доїжджає останній.
      return `warp:${p.track?.id}:${p.clip?.id ?? p.scene?.id}`;
    case 'TrackRoutingSet':
      // Своя адреса на пару (трек, напрям): вхід не перекриває вихід.
      // Подія несе весь маршрут, тож із серії доїжджає останній.
      return `routing:${p.track?.id}:${p.dir}`;
    case 'ClipEnvelopeSet':
      // Подія несе ВЕСЬ конверт параметра, тож із серії рухів олівцем
      // доїжджає останній стан. Адреса -- на трійку (кліп, девайс,
      // параметр): автоматизація Frequency не перекриває автоматизацію
      // Resonance у тому самому кліпі.
      return `env:${p.track?.id}:${p.clip?.id ?? p.scene?.id}`
        + `:${p.chain_path?.map((c) => c?.id).join(',') ?? ''}`
        + `:${p.device?.class_name}:${p.device?.ordinal}:${p.parameter?.ordinal}`;
    case 'ClipPropSet':
      // Своя адреса на пару (кліп, властивість): gain тягнуть мишею, тож
      // серія доїжджає останньою -- але gain не перекриває warp_mode.
      return `clipprop:${p.track?.id}:${p.clip?.id ?? p.scene?.id}:${p.prop}`;
    case 'SceneTimingSet':
      // Увесь блок перевизначень сцени перезаписується цілком, тож серія
      // рухів доїжджає останньою -- як у ClipLoopSet.
      return `scenetiming:${p.scene?.id}`;
    case 'SongPropSet':
      // Кожна властивість -- власна адреса: розмір такту не перекриває
      // тональність, а серія рухів однієї доїжджає останньою.
      return `song:${p.prop}`;
    case 'MixerSet':
      return `mixer:${trackKey(p.track)}:${p.param}:${p.index ?? ''}`;
    case 'TrackToggle':
      return `toggle:${trackKey(p.track)}:${p.param}`;
    case 'ObjectMetaSet':
      return `meta:${p.object}:${trackKey(p.track)}:${p.chain?.id ?? p.clip?.id ?? p.scene?.id ?? ''}:${p.prop}`;
    case 'DeviceParamSet':
      return `device:${trackKey(p.track)}` +
        `:${(p.chain_path || []).map((c) => c.id).join('/')}` +
        `:${p.device?.class_name}/${p.device?.class_display_name}#${p.device?.ordinal}` +
        `:${p.parameter?.name}#${p.parameter?.ordinal}`;
    case 'ClipLoopSet':
      // Межі кліпу перезаписуються цілком, тож серія рухів брекета
      // згортається в останню -- і подія стає відкотною через undo.
      return `loop:${p.track?.id}:${p.clip?.id ?? p.scene?.id}`;
    case 'SlotStopButtonSet':
      // Перемикач слота: остання перемагає, як і будь-який інший перемикач.
      return `stopbtn:${p.track?.id}:${p.scene?.id}`;
    case 'DeviceStateSet':
      // Своя адреса на пару (девайс, властивість): режим програвання не
      // перекриває вибір таблиці, а серія рухів однієї доїжджає останньою.
      return `devstate:${trackKey(p.track)}`
        + `:${(p.chain_path || []).map((c) => c.id).join('/')}`
        + `:${p.device?.class_name}#${p.device?.ordinal}:${p.prop}`;
    case 'SamplePropSet':
      // Своя адреса на пару (девайс, властивість): маркер тягнуть мишею, тож
      // серія доїжджає останньою -- але start_marker не перекриває gain.
      return `sampleprop:${trackKey(p.track)}`
        + `:${(p.chain_path || []).map((c) => c.id).join('/')}`
        + `:${p.device?.class_name}#${p.device?.ordinal}:${p.prop}`;
    case 'ClipNotesSet':
      // Регіон -- частина ключа: подія замінює ноти лише всередині нього,
      // тож два різні регіони одного кліпу не перекривають одне одного.
      return `notes:${p.track?.id}:${p.scene?.id}:${canonical(p.region)}`;
    case 'ArrangementClipMove':
      // Кліп в Arrangement має власний uuid, тож переїзд перезаписує
      // позицію цілком: із серії рухів доїжджає остання.
      return `arrmove:${p.clip?.id}`;
    case 'ArrangementClipNotesSet':
      return `arrnotes:${p.clip?.id}:${canonical(p.region)}`;
    // Далі -- усе, що згортати не можна, і причини в них різні.
    //
    // Структура: подія створює або знищує обʼєкт, тож "остання перемагає"
    // означало б втратити сам обʼєкт, а не проміжне значення. Виконати
    // TrackCreate і не виконати TrackDelete -- це різні сети, а не той самий.
    // NEVER_FOLD: TrackCreate, TrackDelete, TrackDuplicate, SceneCreate, SceneDelete
    // NEVER_FOLD: ClipCreate, ClipDelete, ReturnCreate, ReturnDelete
    // NEVER_FOLD: DeviceInsert, DeviceDelete, DeviceMove, DeviceLoad, SampleLoad
    // NEVER_FOLD: ArrangementClipCreate, ArrangementClipDelete
    //
    // Відтворення: у них своя адреса й свій барʼєр, вони згортаються вище
    // в compactTail, а не тут -- ключ мусить враховувати SceneLaunch.
    // NEVER_FOLD: ClipLaunch, ClipStop, SceneLaunch, StopAllClips
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
