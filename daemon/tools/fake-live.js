// Емулятор bridge: говорить тим самим UDP-протоколом, що й Remote Script,
// але без Live. Потрібен, щоб ганяти daemon+relay та перевіряти порядок подій
// і бутстрап реєстру, не відкриваючи DAW.
//
//   node tools/fake-live.js --udp-in 19845 --udp-out 19846
//
// Команди зі stdin: play | stop | tempo <bpm> | launch <t> <s> | scene <n>
//                   stopclip <t> | stopall | rename <t> <name> | state

import { createSocket } from 'node:dgram';
import { randomBytes } from 'node:crypto';
import { createInterface } from 'node:readline';

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const PORT_DAEMON = Number(arg('udp-in', 19845)); // куди шлемо
const PORT_SELF = Number(arg('udp-out', 19846)); // де слухаємо

// Фейковий стан "проєкту" -- дзеркало того, що тримає справжній bridge.
// id заповнюються на бутстрапі: або генеруємо, або приймаємо чужі.
const song = {
  playing: false,
  tempo: 120,
  tracks: [
    { id: null, name: '1-MIDI', playing_slot_index: -1, slots: 8, mix: {}, mute: false, solo: false, arm: false },
    { id: null, name: '2-MIDI', playing_slot_index: -1, slots: 8, mix: {}, mute: false, solo: false, arm: false },
    { id: null, name: '3-Audio', playing_slot_index: -1, slots: 8, mix: {}, mute: false, solo: false, arm: false },
  ],
  scenes: [0, 1, 2, 3, 4, 5, 6, 7].map((i) => ({ id: null, name: `Scene ${i + 1}` })),
};
let lseq = 0;
let registryReady = false;

const newId = () => randomBytes(6).toString('hex');
const trackById = (id) => song.tracks.find((t) => t.id === id);
const sceneIdx = (id) => song.scenes.findIndex((s) => s.id === id);
const trackRef = (t) => ({ id: t.id, name: t.name });
const sceneRef = (s) => ({ id: s.id });

const udp = createSocket('udp4');
const send = (m) => udp.send(Buffer.from(JSON.stringify(m)), PORT_DAEMON, '127.0.0.1');

// --script і --events дозволяють вдати старіший bridge і перевірити,
// що розсинхрон версій виявляється при конекті
const sendHello = () =>
  send({
    m: 'hello',
    live: arg('live', 'fake-12.3.8'),
    script: arg('script', '0.11.0-fake'),
    pid: process.pid,
    features: ['apply_ack'],
    events: arg('events',
      'TransportSet,TempoSet,ClipLaunch,ClipStop,SceneLaunch,StopAllClips,' +
      'TrackCreate,TrackDelete,SceneCreate,SceneDelete,MixerSet,TrackToggle').split(','),
  });

function emit(type, payload) {
  if (!registryReady && type !== 'TransportSet' && type !== 'TempoSet') {
    return console.log('реєстр ще не готовий — подію не відправлено');
  }
  lseq += 1;
  send({ m: 'event', type, payload, lseq });
  console.log(`-> ${type} ${JSON.stringify(payload)}`);
}

const snapshot = () => ({
  playing: song.playing,
  tempo: song.tempo,
  tracks: song.tracks.map((t, idx) => ({ ...t, idx })),
  scenes: song.scenes.map((s, idx) => ({ ...s, idx })),
});

function buildRegistry() {
  song.tracks.forEach((t) => (t.id = newId()));
  song.scenes.forEach((s) => (s.id = newId()));
  registryReady = true;
  console.log('реєстр створено');
  return {
    tracks: song.tracks.map((t, idx) => ({ id: t.id, idx, name: t.name })),
    scenes: song.scenes.map((s, idx) => ({ id: s.id, idx, name: s.name })),
  };
}

function adoptRegistry(reg) {
  const problems = [];
  for (const [kind, records, objects] of [
    ['трек', reg.tracks || [], song.tracks],
    ['сцена', reg.scenes || [], song.scenes],
  ]) {
    for (const rec of records) {
      const o = objects[rec.idx];
      if (!o) problems.push(`${kind} ${rec.name}: позиції ${rec.idx} немає`);
      else if (rec.name && o.name !== rec.name) problems.push(`${kind} ${rec.idx}: ${o.name} != ${rec.name}`);
      else o.id = rec.id;
    }
  }
  registryReady = true;
  console.log(`реєстр прийнято${problems.length ? `, незіставлено: ${problems.join('; ')}` : ''}`);
}

/** Застосування чужої події. Дзеркало оновлюється мовчки -- саме так
 *  справжній bridge глушить ехо власних listener-ів. */
function apply(type, payload, gseq) {
  const reject = (why) => console.log(`<- #${gseq} ${type} ВІДХИЛЕНО (${why})`);
  switch (type) {
    case 'TransportSet':
      song.playing = !!payload.playing;
      break;
    case 'TempoSet':
      song.tempo = payload.bpm;
      break;
    case 'ClipLaunch': {
      const t = trackById(payload.track?.id);
      const s = sceneIdx(payload.scene?.id);
      if (!t) return reject('невідомий трек');
      if (s < 0) return reject('невідома сцена');
      t.playing_slot_index = s;
      break;
    }
    case 'ClipStop': {
      const t = trackById(payload.track?.id);
      if (!t) return reject('невідомий трек');
      t.playing_slot_index = -1;
      break;
    }
    case 'SceneLaunch': {
      const s = sceneIdx(payload.scene?.id);
      if (s < 0) return reject('невідома сцена');
      for (const t of song.tracks) t.playing_slot_index = s; // у фейку кліпи всюди
      break;
    }
    case 'StopAllClips':
      for (const t of song.tracks) t.playing_slot_index = -1;
      break;
    case 'MixerSet': {
      const t = trackById(payload.track?.id);
      if (!t) return reject('невідомий трек');
      t.mix[`${payload.param}:${payload.index ?? '-'}`] = payload.value;
      break;
    }
    case 'TrackToggle': {
      const t = trackById(payload.track?.id);
      if (!t) return reject('невідомий трек');
      t[payload.param] = !!payload.value;
      break;
    }
    case 'TrackCreate': {
      if (trackById(payload.track?.id)) return reject('такий трек уже є');
      const idx = Number.isInteger(payload.idx) ? payload.idx : song.tracks.length;
      song.tracks.splice(idx, 0, {
        id: payload.track.id,
        name: payload.track.name,
        playing_slot_index: -1,
        slots: song.scenes.length,
      });
      break;
    }
    case 'TrackDelete': {
      const i = song.tracks.findIndex((t) => t.id === payload.track?.id);
      if (i < 0) return reject('трек уже видалений');
      song.tracks.splice(i, 1);
      break;
    }
    case 'SceneCreate': {
      if (sceneIdx(payload.scene?.id) >= 0) return reject('така сцена вже є');
      const idx = Number.isInteger(payload.idx) ? payload.idx : song.scenes.length;
      song.scenes.splice(idx, 0, { id: payload.scene.id, name: payload.scene.name || '' });
      break;
    }
    case 'SceneDelete': {
      const i = sceneIdx(payload.scene?.id);
      if (i < 0) return reject('сцена вже видалена');
      song.scenes.splice(i, 1);
      break;
    }
    default:
      return console.log(`<- #${gseq} невідомий тип ${type}`);
  }
  console.log(`<- #${gseq} ${type} ${JSON.stringify(payload)}`);
}

udp.on('message', (buf) => {
  let msg;
  try {
    msg = JSON.parse(buf.toString('utf8'));
  } catch {
    return;
  }
  if (msg.m === 'hello_request') sendHello();
  else if (msg.m === 'apply') {
    try {
      apply(msg.type, msg.payload, msg.gseq);
      send({ m: 'apply_ack', gseq: msg.gseq, ok: true });
    } catch (error) {
      send({ m: 'apply_ack', gseq: msg.gseq, ok: false, error: error.message });
    }
  }
  else if (msg.m === 'snapshot_request') send({ m: 'snapshot', state: snapshot() });
  else if (msg.m === 'registry_build') send({ m: 'registry', registry: buildRegistry() });
  else if (msg.m === 'registry_adopt') adoptRegistry(msg.registry || {});
  else if (msg.m === 'ping') send({ m: 'heartbeat', t: Date.now() / 1000 });
});

udp.bind(PORT_SELF, '127.0.0.1', () => {
  console.log(`fake-live: слухаю :${PORT_SELF}, шлю на :${PORT_DAEMON}`);
  sendHello();
  send({ m: 'snapshot', state: snapshot() });
  setInterval(() => send({ m: 'heartbeat', t: Date.now() / 1000 }), 2000);
});

createInterface({ input: process.stdin }).on('line', (line) => {
  const [cmd, ...rest] = line.trim().split(/\s+/);
  const track = () => song.tracks[Number(rest[0]) || 0];
  switch (cmd) {
    case 'play':
      song.playing = true;
      emit('TransportSet', { playing: true });
      break;
    case 'stop':
      song.playing = false;
      emit('TransportSet', { playing: false });
      break;
    case 'tempo':
      song.tempo = Number(rest[0]);
      emit('TempoSet', { bpm: song.tempo });
      break;
    case 'launch': {
      const t = track();
      const s = song.scenes[Number(rest[1]) || 0];
      if (!t || !s) return console.log('немає такого треку/сцени');
      t.playing_slot_index = song.scenes.indexOf(s);
      emit('ClipLaunch', { track: trackRef(t), scene: sceneRef(s) });
      break;
    }
    case 'scene': {
      const s = song.scenes[Number(rest[0]) || 0];
      if (!s) return console.log('немає такої сцени');
      for (const t of song.tracks) t.playing_slot_index = song.scenes.indexOf(s);
      emit('SceneLaunch', { scene: sceneRef(s) });
      break;
    }
    case 'stopclip': {
      const t = track();
      if (!t) return console.log('немає такого треку');
      t.playing_slot_index = -1;
      emit('ClipStop', { track: trackRef(t) });
      break;
    }
    case 'stopall':
      for (const t of song.tracks) t.playing_slot_index = -1;
      emit('StopAllClips', {});
      break;
    case 'addtrack': {
      const kind = rest[0] === 'audio' ? 'audio' : 'midi';
      const idx = Number.isInteger(Number(rest[1])) && rest[1] !== undefined ? Number(rest[1]) : song.tracks.length;
      const t = { id: newId(), name: `${idx + 1}-${kind === 'midi' ? 'MIDI' : 'Audio'}`, playing_slot_index: -1, slots: song.scenes.length };
      song.tracks.splice(idx, 0, t);
      emit('TrackCreate', { track: { id: t.id, name: t.name }, idx, kind });
      break;
    }
    case 'deltrack': {
      const t = track();
      if (!t) return console.log('немає такого треку');
      song.tracks.splice(song.tracks.indexOf(t), 1);
      emit('TrackDelete', { track: { id: t.id } });
      break;
    }
    case 'addscene': {
      const idx = rest[0] !== undefined ? Number(rest[0]) : song.scenes.length;
      const s = { id: newId(), name: '' };
      song.scenes.splice(idx, 0, s);
      emit('SceneCreate', { scene: { id: s.id }, idx });
      break;
    }
    case 'delscene': {
      const s = song.scenes[Number(rest[0]) || 0];
      if (!s) return console.log('немає такої сцени');
      song.scenes.splice(song.scenes.indexOf(s), 1);
      emit('SceneDelete', { scene: { id: s.id } });
      break;
    }
    case 'vol':
    case 'pan':
    case 'send': {
      const t = track();
      if (!t) return console.log('немає такого треку');
      const param = cmd === 'vol' ? 'volume' : cmd === 'pan' ? 'panning' : 'send';
      const idx = param === 'send' ? Number(rest[1]) : null;
      const value = Number(param === 'send' ? rest[2] : rest[1]);
      t.mix[`${param}:${idx ?? '-'}`] = value;
      const payload = { track: { id: t.id }, param, value };
      if (idx !== null) payload.index = idx;
      emit('MixerSet', payload);
      break;
    }
    case 'mute':
    case 'solo':
    case 'arm': {
      const t = track();
      if (!t) return console.log('немає такого треку');
      t[cmd] = !t[cmd];
      emit('TrackToggle', { track: { id: t.id }, param: cmd, value: t[cmd] });
      break;
    }
    case 'rename': {
      // перевірка, що uuid переживає те, від чого ламалась адресація за індексом
      const t = track();
      if (!t) return console.log('немає такого треку');
      t.name = rest.slice(1).join(' ');
      console.log(`трек перейменовано на ${t.name}, id незмінний: ${t.id}`);
      break;
    }
    case 'move': {
      // переставляє трек -- індекси їдуть, uuid лишаються
      const from = Number(rest[0]);
      const to = Number(rest[1]);
      if (!song.tracks[from] || !song.tracks[to]) return console.log('немає таких треків');
      const [t] = song.tracks.splice(from, 1);
      song.tracks.splice(to, 0, t);
      console.log(`трек ${t.name} тепер на позиції ${to}, id незмінний: ${t.id}`);
      break;
    }
    case 'state':
      console.log(JSON.stringify(snapshot(), null, 2));
      break;
    case '':
      break;
    default:
      console.log([
        'play | stop | tempo <bpm>',
        'launch <t> <s> | scene <n> | stopclip <t> | stopall',
        'addtrack [midi|audio] [idx] | deltrack <t> | addscene [idx] | delscene <n>',
        'rename <t> <name> | move <from> <to> | state',
      ].join('\n'));
  }
});
