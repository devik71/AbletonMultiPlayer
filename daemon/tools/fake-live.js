// Емулятор bridge: говорить тим самим UDP-протоколом, що й Remote Script,
// але без Live. Потрібен, щоб ганяти daemon+relay та перевіряти порядок подій,
// не відкриваючи DAW.
//
//   node tools/fake-live.js --udp-in 19845 --udp-out 19846
//
// Команди зі stdin: play | stop | tempo 128 | launch <track> <scene> | stopclip <track> | state

import { createSocket } from 'node:dgram';
import { createInterface } from 'node:readline';

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const PORT_DAEMON = Number(arg('udp-in', 19845)); // куди шлемо
const PORT_SELF = Number(arg('udp-out', 19846)); // де слухаємо

// Фейковий стан "проєкту" -- дзеркало того, що тримає справжній bridge
const song = {
  playing: false,
  tempo: 120,
  tracks: [
    { idx: 0, name: '1-MIDI', playing_slot_index: -1, slots: 8 },
    { idx: 1, name: '2-MIDI', playing_slot_index: -1, slots: 8 },
    { idx: 2, name: '3-Audio', playing_slot_index: -1, slots: 8 },
  ],
  scenes: [0, 1, 2, 3, 4, 5, 6, 7].map((i) => ({ idx: i, name: `Scene ${i + 1}` })),
};
let lseq = 0;

const udp = createSocket('udp4');
const send = (m) => udp.send(Buffer.from(JSON.stringify(m)), PORT_DAEMON, '127.0.0.1');

function emit(type, payload) {
  lseq += 1;
  send({ m: 'event', type, payload, lseq });
  console.log(`-> ${type} ${JSON.stringify(payload)}`);
}

const snapshot = () => ({ playing: song.playing, tempo: song.tempo, tracks: song.tracks, scenes: song.scenes });
const trackRef = (t) => ({ idx: t.idx, name: t.name });

/** Застосування чужої події. Дзеркало оновлюється мовчки -- саме так
 *  справжній bridge глушить ехо власних listener-ів. */
function apply(type, payload, gseq) {
  switch (type) {
    case 'TransportSet':
      song.playing = !!payload.playing;
      break;
    case 'TempoSet':
      song.tempo = payload.bpm;
      break;
    case 'ClipLaunch': {
      const t = song.tracks[payload.track?.idx];
      if (!t || t.name !== payload.track.name) return console.log(`<- #${gseq} ${type} ВІДХИЛЕНО (розсинхрон треку)`);
      t.playing_slot_index = payload.scene.idx;
      break;
    }
    case 'ClipStop': {
      const t = song.tracks[payload.track?.idx];
      if (!t || t.name !== payload.track.name) return console.log(`<- #${gseq} ${type} ВІДХИЛЕНО (розсинхрон треку)`);
      t.playing_slot_index = -1;
      break;
    }
    case 'StopAllClips':
      for (const t of song.tracks) t.playing_slot_index = -1;
      break;
    case 'SceneLaunch': {
      const s = song.scenes[payload.scene?.idx];
      if (!s) return console.log(`<- #${gseq} ${type} ВІДХИЛЕНО (немає сцени)`);
      // у фейку кліпи є всюди, тож сцену грають усі треки
      for (const t of song.tracks) t.playing_slot_index = s.idx;
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
  if (msg.m === 'apply') apply(msg.type, msg.payload, msg.gseq);
  else if (msg.m === 'snapshot_request') send({ m: 'snapshot', state: snapshot() });
  else if (msg.m === 'ping') send({ m: 'heartbeat', t: Date.now() / 1000 });
});

udp.bind(PORT_SELF, '127.0.0.1', () => {
  console.log(`fake-live: слухаю :${PORT_SELF}, шлю на :${PORT_DAEMON}`);
  send({ m: 'hello', live: 'fake-12.3.8', script: '0.1.0-fake', pid: process.pid });
  send({ m: 'snapshot', state: snapshot() });
  setInterval(() => send({ m: 'heartbeat', t: Date.now() / 1000 }), 2000);
});

createInterface({ input: process.stdin }).on('line', (line) => {
  const [cmd, ...rest] = line.trim().split(/\s+/);
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
      const t = song.tracks[Number(rest[0]) || 0];
      const s = song.scenes[Number(rest[1]) || 0];
      if (!t || !s) return console.log('немає такого треку/сцени');
      t.playing_slot_index = s.idx;
      emit('ClipLaunch', { track: trackRef(t), scene: { idx: s.idx, name: s.name } });
      break;
    }
    case 'stopall':
      for (const t of song.tracks) t.playing_slot_index = -1;
      emit('StopAllClips', {});
      break;
    case 'scene': {
      const s = song.scenes[Number(rest[0]) || 0];
      if (!s) return console.log('немає такої сцени');
      for (const t of song.tracks) t.playing_slot_index = s.idx;
      emit('SceneLaunch', { scene: { idx: s.idx } });
      break;
    }
    case 'stopclip': {
      const t = song.tracks[Number(rest[0]) || 0];
      if (!t) return console.log('немає такого треку');
      t.playing_slot_index = -1;
      emit('ClipStop', { track: trackRef(t) });
      break;
    }
    case 'state':
      console.log(JSON.stringify(snapshot(), null, 2));
      break;
    case '':
      break;
    default:
      console.log('play | stop | tempo <bpm> | launch <t> <s> | scene <n> | stopclip <t> | stopall | state');
  }
});
