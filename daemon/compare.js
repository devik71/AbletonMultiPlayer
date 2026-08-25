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
    }

    const myArr = (my.arrangement || []).length;
    const theirArr = (their.arrangement || []).length;
    if (myArr !== theirArr) {
      add(`${where}: кліпів у лінійці ${myArr} проти ${theirArr}`);
    }
  }

  return out;
}
