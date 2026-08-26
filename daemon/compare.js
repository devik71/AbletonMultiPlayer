// Порівняння двох знімків стану -- читанням, без запису.
//
// Досі єдиний спосіб дізнатись, чи машини зійшлися, був `pull`: він тягне
// чужий знімок і ЗАСТОСОВУЄ його. Тобто діагностика змінювала стан, і після
// неї вже не можна було сказати, що саме розходилось. Тут навпаки: беремо
// обидва знімки й називаємо розбіжності.

const byId = (list) => new Map((list || []).filter((x) => x?.id).map((x) => [x.id, x]));
const clipKey = (c) => c?.scene?.id;
const num = (v) => (typeof v === 'number' ? Math.round(v * 1e6) / 1e6 : v);

const nameOf = (obj, fallback) => (obj?.name ? `«${obj.name}»` : fallback);

/** Ключ девайса в дереві: клас, порядковий номер і шлях ланцюга. */
const deviceKey = (d) => [(d.chain_path || []).map((c) => c.id).join('/'),
  d.device?.class_display_name, d.device?.ordinal].join('#');

/** Значення параметрів девайса за іменем і порядковим номером. */
const paramMap = (d) => new Map((d.parameters || [])
  .map((p) => [`${p.name}#${p.ordinal ?? 0}`, num(p.value)]));

/** Ті самі семпли, розтягнуті по-різному, звучать по-різному. */
const warpKey = (list) => (list || []).map((m) => `${num(m.beat_time)}:${num(m.sample_time)}`).join(',');

/** Межі кліпу цілком: петля й маркери -- це пʼять полів, які має сенс
 *  порівнювати разом, бо поодинці вони нічого не означають. */
const LOOP_PROPS = ['looping', 'loop_start', 'loop_end', 'start_marker', 'end_marker'];
const loopKey = (l) => (l ? LOOP_PROPS.map((p) => `${p}=${num(l[p])}`).join(' ') : '');

/** Мікшер: гучність, панорама й перемикачі. Досі `diff` мовчав про них
 *  узагалі -- порівнювались лише сенди, тобто розʼїхані фейдери двох машин
 *  звіт називав повним збігом. */
const MIX_SCALARS = ['volume', 'panning', 'crossfader', 'cue_volume', 'crossfade_assign'];
const MIX_TOGGLES = ['mute', 'solo', 'arm'];
function mixerDiff(where, my, their, add) {
  const a = my?.mixer || {};
  const b = their?.mixer || {};
  for (const param of MIX_SCALARS) {
    if (a[param] === undefined && b[param] === undefined) continue;
    if (num(a[param]) === num(b[param])) continue;
    add(`${where}: ${param} ${a[param] ?? '—'} проти ${b[param] ?? '—'}`);
  }
  for (const param of MIX_TOGGLES) {
    if (a[param] === undefined && b[param] === undefined) continue;
    if (!!a[param] === !!b[param]) continue;
    add(`${where}: ${param} ${a[param] ? 'увімкнено' : 'вимкнено'}`
      + ` проти ${b[param] ? 'увімкненого' : 'вимкненого'}`);
  }
  const mySends = new Map((a.sends || []).map((x) => [x.index, num(x.value)]));
  const theirSends = new Map((b.sends || []).map((x) => [x.index, num(x.value)]));
  for (const [idx, value] of mySends) {
    if (theirSends.has(idx) && theirSends.get(idx) !== value) {
      add(`${where}: сенд ${idx} ${value} проти ${theirSends.get(idx)}`);
    }
  }
}

/** Канонічний рядок плоского блоку: порядок ключів не має значення. */
const loopKeyless = (o) => (o ? Object.keys(o).sort().map((k) => `${k}=${num(o[k])}`).join(' ') : '');

/** Позиція в долях, а поруч у тактах -- якщо розмір такту відомий.
 *
 *  Доля в LOM -- чверть незалежно від знаменника, тож такт займає
 *  numerator * 4 / denominator долі: у 6/8 це три долі, а не шість.
 *  Такт саме ПОРУЧ, а не замість: доля -- те, що реально лежить у знімку. */
/** Девайси й їхні параметри. Викликається і для звичайних треків, і для
 *  Return/Master: ревер на Return, зведений по-різному, чути в усьому сеті,
 *  а порівнювались досі лише звичайні треки. */
function deviceDiff(where, my, their, add) {
  const myDev = (my.devices || []).map((d) => d.device?.class_display_name).join(', ');
  const theirDev = (their.devices || []).map((d) => d.device?.class_display_name).join(', ');
  if (myDev !== theirDev) {
    add(`${where}: девайси у тебе [${myDev || '—'}], у партнера [${theirDev || '—'}]`);
  }

  // Однакова структура з різними значеннями -- найгірша розбіжність:
  // сети виглядають однаково, а звучать по-різному.
  const myDevMap = new Map((my.devices || []).map((d) => [deviceKey(d), d]));
  const theirDevMap = new Map((their.devices || []).map((d) => [deviceKey(d), d]));
  for (const [key, mine] of myDevMap) {
    const other = theirDevMap.get(key);
    if (!other) continue;
    const a = paramMap(mine);
    const b = paramMap(other);
    const differing = [...a].filter(([name, value]) => b.has(name) && b.get(name) !== value);
    if (!differing.length) continue;
    const shown = differing.slice(0, 3)
      .map(([name, value]) => `${name.split('#')[0]} ${value}≠${b.get(name)}`);
    add(`${where}, ${mine.device?.class_display_name}: розходяться ${differing.length} параметрів`
      + ` (${shown.join(', ')})`);
  }
}

export const positionWith = (song) => {
  const bar = (value) => {
    const n = song?.signature_numerator;
    const d = song?.signature_denominator;
    if (!(n > 0) || !(d > 0)) return '';
    const barBeats = (n * 4) / d;
    if (!(barBeats > 0)) return '';
    const index = Math.floor(value / barBeats) + 1;
    const within = value - (index - 1) * barBeats;
    return ` (такт ${index}.${+(within + 1).toFixed(2)})`;
  };
  // Два відмінки, бо рядки читає людина: «на 16-й долі», але «проти 32-ї долі».
  const form = (ending) => (beats) => {
    const value = num(beats);
    if (typeof value !== 'number') return String(beats);
    return `${value}-${ending} долі${bar(value)}`;
  };
  return { at: form('й'), gen: form('ї') };
};

/** Розбіжності між моїм і чужим знімком. Порожній масив -- усе збігається. */
export function compareStates(mine, theirs, { limit = 40 } = {}) {
  const out = [];
  // Збираємо все (до розумної стелі), а обрізаємо в кінці: інакше
  // не скажеш, скільки розбіжностей лишилось за кадром, а «ще 200»
  // і «більше немає» -- дуже різні новини.
  const HARD_CAP = 2000;
  const add = (line) => { if (out.length < HARD_CAP) out.push(line); };
  // Розмір такту беремо зі СВОГО знімка: він і є системою координат читача.
  const { at, gen } = positionWith(mine?.song);

  if (num(mine?.tempo) !== num(theirs?.tempo)) {
    add(`темп: у тебе ${mine?.tempo}, у партнера ${theirs?.tempo}`);
  }

  const myProps = mine?.song || {};
  const theirProps = theirs?.song || {};
  for (const prop of new Set([...Object.keys(myProps), ...Object.keys(theirProps)])) {
    if (num(myProps[prop]) !== num(theirProps[prop])) {
      add(`${prop}: у тебе ${myProps[prop] ?? '—'}, у партнера ${theirProps[prop] ?? '—'}`);
    }
  }

  const myCues = new Map((mine?.cues || []).map((c) => [num(c.time), c.name || '']));
  const theirCues = new Map((theirs?.cues || []).map((c) => [num(c.time), c.name || '']));
  for (const [time, name] of theirCues) {
    if (!myCues.has(time)) add(`локатор «${name}» на ${at(time)} є в партнера, у тебе немає`);
  }
  for (const [time, name] of myCues) {
    if (!theirCues.has(time)) add(`локатор «${name}» на ${at(time)} є в тебе, у партнера немає`);
    else if (theirCues.get(time) !== name) {
      add(`локатор на ${at(time)}: у тебе «${name}», у партнера «${theirCues.get(time)}»`);
    }
  }

  for (const [kind, key] of [['трек', 'tracks'], ['сцена', 'scenes']]) {
    const my = byId(mine?.[key]);
    const their = byId(theirs?.[key]);
    for (const [id, obj] of their) {
      if (!my.has(id)) add(`${kind} ${nameOf(obj, id)} є в партнера, у тебе немає`);
    }
    for (const [id, obj] of my) {
      if (!their.has(id)) add(`${kind} ${nameOf(obj, id)} є в тебе, у партнера немає`);
      else {
        const other = their.get(id);
        if (obj.name !== other.name) {
          add(`${kind} ${id}: у тебе «${obj.name}», у партнера «${other.name}»`);
        }
        if ((obj.color ?? null) !== (other.color ?? null)) {
          add(`${kind} ${nameOf(obj, id)}: колір ${obj.color ?? '—'} проти ${other.color ?? '—'}`);
        }
        // Темп сцени мовчки перемикає темп в одного і не перемикає в іншого
        if (key === 'scenes' && loopKeyless(obj.timing) !== loopKeyless(other.timing)) {
          add(`сцена ${nameOf(obj, id)}: темп/метр сцени різні`);
        }
      }
    }
  }

  // Return-треки окремо: саме їхній набір визначає, ЩО означає index сенда.
  // Розбіжність тут не косметична -- вона робить той самий сенд іншим ревером.
  const myAux = byId(mine?.aux_tracks);
  const theirAux = byId(theirs?.aux_tracks);
  for (const [id, obj] of theirAux) {
    if (!myAux.has(id)) add(`${obj.kind || "aux"} ${nameOf(obj, id)} є в партнера, у тебе немає`);
  }
  for (const [id, obj] of myAux) {
    if (!theirAux.has(id)) {
      add(`${obj.kind || "aux"} ${nameOf(obj, id)} є в тебе, у партнера немає`);
      continue;
    }
    // Гучність Return -- це те, наскільки чути ревер у всьому сеті одразу
    const auxWhere = `${obj.kind || 'aux'} ${nameOf(obj, id)}`;
    mixerDiff(auxWhere, obj, theirAux.get(id), add);
    deviceDiff(auxWhere, obj, theirAux.get(id), add);
  }
  const myReturns = (mine?.aux_tracks || []).filter((t) => t.kind === "return").length;
  const theirReturns = (theirs?.aux_tracks || []).filter((t) => t.kind === "return").length;
  if (myReturns !== theirReturns) {
    add(`Return-треків ${myReturns} проти ${theirReturns} — індекси сендів означають різне`);
  }

  // Ланцюги йдуть ПІСЛЯ структури навмисно. У Drum Rack їх десятки, і кожен
  // дає до шести рядків -- при стелі звіту вони витіснили б те, заради чого
  // звіт і читають: зниклий трек, чужу назву, розʼїханий Return.
  const myChains = new Map((mine?.chains || []).map((c) => [c.id, c]));
  const theirChains = new Map((theirs?.chains || []).map((c) => [c.id, c]));
  for (const [id, my] of myChains) {
    const their = theirChains.get(id);
    if (!their) continue;
    for (const param of ['volume', 'panning', 'mute', 'solo']) {
      if (num(my[param]) === num(their[param])) continue;
      add(`ланцюг ${id}: ${param} ${my[param] ?? '—'} проти ${their[param] ?? '—'}`);
    }
    // Назва пада -- те, за чим людина його впізнає: «Kick» проти «Chain 1»
    // виглядає як інший інструмент ще до того, як його почули.
    for (const prop of ['name', 'color']) {
      if ((my[prop] ?? null) === (their[prop] ?? null)) continue;
      add(`ланцюг ${id}: ${prop} ${my[prop] ?? '—'} проти ${their[prop] ?? '—'}`);
    }
  }

  const myTracks = byId(mine?.tracks);
  const theirTracks = byId(theirs?.tracks);
  for (const [id, my] of myTracks) {
    const their = theirTracks.get(id);
    if (!their) continue;
    const where = nameOf(my, id);

    deviceDiff(where, my, their, add);

    mixerDiff(where, my, their, add);

    // Стоп-кнопка порожнього слота вирішує, чи зупинить трек запуск сцени:
    // розбіжність тут чути не в мікшері, а в тому, що сет грає інакше.
    const myStop = new Set(my.stop_off || []);
    const theirStop = new Set(their.stop_off || []);
    for (const sid of myStop) {
      if (!theirStop.has(sid)) add(`${where}, сцена ${sid}: стоп-кнопки немає в тебе, у партнера є`);
    }
    for (const sid of theirStop) {
      if (!myStop.has(sid)) add(`${where}, сцена ${sid}: стоп-кнопка є в тебе, у партнера немає`);
    }

    const myClips = new Map((my.clips || []).map((c) => [clipKey(c), c]));
    const theirClips = new Map((their.clips || []).map((c) => [clipKey(c), c]));
    for (const [scene, clip] of theirClips) {
      if (!myClips.has(scene)) add(`${where}: кліп у сцені ${scene} є в партнера, у тебе немає`);
    }
    for (const [scene, clip] of myClips) {
      const other = theirClips.get(scene);
      if (!other) { add(`${where}: кліп у сцені ${scene} є в тебе, у партнера немає`); continue; }
      if (num(clip.clip?.length) !== num(other.clip?.length)) {
        add(`${where}, сцена ${scene}: довжина ${clip.clip?.length} проти ${other.clip?.length}`);
      }
      const mineNotes = (clip.notes || []).length;
      const theirNotes = (other.notes || []).length;
      if (mineNotes !== theirNotes) {
        add(`${where}, сцена ${scene}: нот ${mineNotes} проти ${theirNotes}`);
      }
      for (const prop of new Set([...Object.keys(clip.props || {}),
                                  ...Object.keys(other.props || {})])) {
        if (num(clip.props?.[prop]) !== num(other.props?.[prop])) {
          add(`${where}, сцена ${scene}: ${prop} ${clip.props?.[prop] ?? '—'}`
            + ` проти ${other.props?.[prop] ?? '—'}`);
        }
      }
      if (warpKey(clip.warp) !== warpKey(other.warp)) {
        add(`${where}, сцена ${scene}: warp-маркери різні`
          + ` (${(clip.warp || []).length} проти ${(other.warp || []).length})`);
      }
      if (loopKey(clip.loop) !== loopKey(other.loop)) {
        add(`${where}, сцена ${scene}: межі кліпу різні`);
      }
      for (const prop of ['name', 'color']) {
        const a = clip.clip?.[prop];
        const b = other.clip?.[prop];
        if ((a ?? null) === (b ?? null)) continue;
        add(`${where}, сцена ${scene}: ${prop} ${a ?? '—'} проти ${b ?? '—'}`);
      }
    }

    // Лінійка -- це вже аранжування, тож розбіжність тут чути найдужче.
    // Порівнюємо не лише кількість, а кожен кліп за його uuid.
    const myArrMap = new Map((my.arrangement || []).filter((c) => c.id).map((c) => [c.id, c]));
    const theirArrMap = new Map((their.arrangement || []).filter((c) => c.id).map((c) => [c.id, c]));
    for (const [id, clip] of theirArrMap) {
      if (!myArrMap.has(id)) {
        add(`${where}: кліп у лінійці на ${at(clip.start_time)} є в партнера, у тебе немає`);
      }
    }
    for (const [id, clip] of myArrMap) {
      const other = theirArrMap.get(id);
      if (!other) {
        add(`${where}: кліп у лінійці на ${at(clip.start_time)} є в тебе, у партнера немає`);
        continue;
      }
      if (num(clip.start_time) !== num(other.start_time)) {
        add(`${where}: кліп у лінійці на ${at(clip.start_time)} проти ${gen(other.start_time)}`);
      }
      for (const prop of new Set([...Object.keys(clip.props || {}),
                                  ...Object.keys(other.props || {})])) {
        if (num(clip.props?.[prop]) !== num(other.props?.[prop])) {
          add(`${where}, лінійка: ${prop} ${clip.props?.[prop] ?? '—'} проти ${other.props?.[prop] ?? '—'}`);
        }
      }
      if (warpKey(clip.warp) !== warpKey(other.warp)) {
        add(`${where}, лінійка: warp-маркери різні`);
      }
      if (num(clip.length) !== num(other.length)) {
        add(`${where}, лінійка: довжина ${num(clip.length)} проти ${num(other.length)}`);
      }
      if (loopKey(clip.loop) !== loopKey(other.loop)) {
        add(`${where}, лінійка: межі кліпу різні`);
      }
      const mineN = (clip.notes || []).length;
      const theirN = (other.notes || []).length;
      if (mineN !== theirN) add(`${where}, лінійка: нот ${mineN} проти ${theirN}`);
    }
  }

  if (out.length <= limit) return out;
  const rest = out.length - limit;
  return out.slice(0, limit).concat([
    `…і ще ${rest}${out.length >= HARD_CAP ? " або більше" : ""} розбіжностей`,
  ]);
}
