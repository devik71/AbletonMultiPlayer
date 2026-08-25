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

/** Розбіжності між моїм і чужим знімком. Порожній масив -- усе збігається. */
export function compareStates(mine, theirs, { limit = 40 } = {}) {
  const out = [];
  const add = (line) => { if (out.length < limit) out.push(line); };

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
    if (!myCues.has(time)) add(`локатор «${name}» на ${time}-й долі є в партнера, у тебе немає`);
  }
  for (const [time, name] of myCues) {
    if (!theirCues.has(time)) add(`локатор «${name}» на ${time}-й долі є в тебе, у партнера немає`);
    else if (theirCues.get(time) !== name) {
      add(`локатор на ${time}-й долі: у тебе «${name}», у партнера «${theirCues.get(time)}»`);
    }
  }

  // Ланцюги: у Drum Rack це гучність кожного пада
  const myChains = new Map((mine?.chains || []).map((c) => [c.id, c]));
  const theirChains = new Map((theirs?.chains || []).map((c) => [c.id, c]));
  for (const [id, my] of myChains) {
    const their = theirChains.get(id);
    if (!their) continue;
    for (const param of ['volume', 'panning', 'mute', 'solo']) {
      if (num(my[param]) === num(their[param])) continue;
      add(`ланцюг ${id}: ${param} ${my[param] ?? '—'} проти ${their[param] ?? '—'}`);
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
      else if (obj.name !== their.get(id).name) {
        add(`${kind} ${id}: у тебе «${obj.name}», у партнера «${their.get(id).name}»`);
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
    if (!theirAux.has(id)) add(`${obj.kind || "aux"} ${nameOf(obj, id)} є в тебе, у партнера немає`);
  }
  const myReturns = (mine?.aux_tracks || []).filter((t) => t.kind === "return").length;
  const theirReturns = (theirs?.aux_tracks || []).filter((t) => t.kind === "return").length;
  if (myReturns !== theirReturns) {
    add(`Return-треків ${myReturns} проти ${theirReturns} — індекси сендів означають різне`);
  }

  const myTracks = byId(mine?.tracks);
  const theirTracks = byId(theirs?.tracks);
  for (const [id, my] of myTracks) {
    const their = theirTracks.get(id);
    if (!their) continue;
    const where = nameOf(my, id);

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
      const shown = differing.slice(0, 3).map(([name, value]) => `${name.split('#')[0]} ${value}≠${b.get(name)}`);
      add(`${where}, ${mine.device?.class_display_name}: розходяться ${differing.length} параметрів`
        + ` (${shown.join(
)})`);
    }

    const mySends = new Map((my.mixer?.sends || []).map((x) => [x.index, num(x.value)]));
    const theirSends = new Map((their.mixer?.sends || []).map((x) => [x.index, num(x.value)]));
    for (const [idx, value] of mySends) {
      if (theirSends.has(idx) && theirSends.get(idx) !== value) {
        add(`${where}: сенд ${idx} ${value} проти ${theirSends.get(idx)}`);
      }
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
    }

    // Лінійка -- це вже аранжування, тож розбіжність тут чути найдужче.
    // Порівнюємо не лише кількість, а кожен кліп за його uuid.
    const myArrMap = new Map((my.arrangement || []).filter((c) => c.id).map((c) => [c.id, c]));
    const theirArrMap = new Map((their.arrangement || []).filter((c) => c.id).map((c) => [c.id, c]));
    for (const [id, clip] of theirArrMap) {
      if (!myArrMap.has(id)) {
        add(`${where}: кліп у лінійці на ${num(clip.start_time)}-й долі є в партнера, у тебе немає`);
      }
    }
    for (const [id, clip] of myArrMap) {
      const other = theirArrMap.get(id);
      if (!other) {
        add(`${where}: кліп у лінійці на ${num(clip.start_time)}-й долі є в тебе, у партнера немає`);
        continue;
      }
      if (num(clip.start_time) !== num(other.start_time)) {
        add(`${where}: кліп у лінійці на ${num(clip.start_time)}-й долі проти ${num(other.start_time)}-ї`);
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
      const mineN = (clip.notes || []).length;
      const theirN = (other.notes || []).length;
      if (mineN !== theirN) add(`${where}, лінійка: нот ${mineN} проти ${theirN}`);
    }
  }

  return out;
}
