// Емулятор bridge: говорить тим самим UDP-протоколом, що й Remote Script,
// але без Live. Потрібен, щоб ганяти daemon+relay та перевіряти порядок подій
// і бутстрап реєстру, не відкриваючи DAW.
//
//   node tools/fake-live.js --udp-in 19845 --udp-out 19846
//
// Команди зі stdin: play | stop | tempo <bpm> | launch <t> <s> | scene <n>
//                   stopclip <t> | stopall | note <t> <s> <pitch> <start> <duration> <velocity>
//                   delnote <t> <s> <pitch> <start> | delclip <t> <s>
//                   mix <track|return:N|master> <parameter> <value> [send-index]
//                   toggle <track|return:N> <mute|solo|arm> | state
//                   device <track|return:N|master> <device[/chain/device...]> <parameter> <value>

import { createSocket } from 'node:dgram';
import { createHash, randomBytes } from 'node:crypto';
import { createInterface } from 'node:readline';

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const PORT_DAEMON = Number(arg('udp-in', 19845)); // куди шлемо
const PORT_SELF = Number(arg('udp-out', 19846)); // де слухаємо
const NOTE_TIME_SPAN = 4;
const NOTE_PITCH_SPAN = 16;

const emptyClips = (count) => Array.from({ length: count }, () => null);
const fakeParam = (name, value = 0.5, isQuantized = false) =>
  ({ name, value, min: 0, max: 1, is_quantized: isQuantized });
const fakeFilter = (value = 0.5) => ({
  class_name: 'AutoFilter',
  class_display_name: 'Auto Filter',
  parameters: [fakeParam('Frequency', value)],
});
const fakeNestedRack = () => ({
  class_name: 'AudioEffectGroupDevice',
  class_display_name: 'Audio Effect Rack',
  parameters: [fakeParam('Macro 1', 0.5)],
  chains: [{ id: null, name: 'Inner Chain', devices: [fakeFilter(0.35)] }],
  return_chains: [],
});
const fakeRack = () => ({
  class_name: 'AudioEffectGroupDevice',
  class_display_name: 'Audio Effect Rack',
  parameters: [fakeParam('Macro 1', 0.5)],
  // Duplicate names are intentional: identity must not depend on name alone.
  chains: [
    { id: null, name: 'Chain', devices: [fakeFilter(0.25)] },
    { id: null, name: 'Chain', devices: [fakeNestedRack()] },
  ],
  return_chains: [],
});
const fakeDevices = () => [
  {
    class_name: 'Operator',
    class_display_name: 'Operator',
    parameters: [
      { name: 'Device On', value: 1, min: 0, max: 1, is_quantized: true },
      { name: 'Filter Freq', value: 0.5, min: 0, max: 1, is_quantized: false },
    ],
  },
  {
    class_name: 'AutoFilter',
    class_display_name: 'Auto Filter',
    parameters: [{ name: 'Frequency', value: 0.4, min: 0, max: 1, is_quantized: false }],
  },
  {
    class_name: 'AutoFilter',
    class_display_name: 'Auto Filter',
    parameters: [{ name: 'Frequency', value: 0.6, min: 0, max: 1, is_quantized: false }],
  },
  fakeRack(),
];

// Фейковий стан "проєкту" -- дзеркало того, що тримає справжній bridge.
// id заповнюються на бутстрапі: або генеруємо, або приймаємо чужі.
const song = {
  playing: false,
  tempo: 120,
  tracks: [
    { id: null, name: '1-MIDI', playing_slot_index: -1, slots: 8, clips: emptyClips(8), devices: fakeDevices(), mix: {}, mute: false, solo: false, arm: false },
    { id: null, name: '2-MIDI', playing_slot_index: -1, slots: 8, clips: emptyClips(8), devices: fakeDevices(), mix: {}, mute: false, solo: false, arm: false },
    { id: null, name: '3-Audio', playing_slot_index: -1, slots: 8, clips: emptyClips(8), devices: fakeDevices(), mix: {}, mute: false, solo: false, arm: false },
  ],
  return_tracks: [
    { id: null, kind: 'return', name: 'A-Return', devices: [fakeFilter(0.2), fakeRack()], mix: {}, mute: false, solo: false },
    { id: null, kind: 'return', name: 'B-Return', devices: [fakeFilter(0.3)], mix: {}, mute: false, solo: false },
  ],
  master_track: { id: null, kind: 'master', name: 'Master', devices: [fakeFilter(0.7), fakeRack()], mix: {} },
  scenes: [0, 1, 2, 3, 4, 5, 6, 7].map((i) => ({ id: null, name: `Scene ${i + 1}` })),
};
let lseq = 0;
let registryReady = false;

const newId = () => randomBytes(6).toString('hex');
const trackById = (id) => song.tracks.find((t) => t.id === id);
const auxTracks = () => [...song.return_tracks, song.master_track];
const allDeviceTracks = () => [...song.tracks, ...auxTracks()];
const auxTrackRef = (t) => ({ id: t.id, kind: t.kind });
const deviceTrackRef = (t) => t.kind ? auxTrackRef(t) : { id: t.id };
const deviceTrackByRef = (ref) => {
  if (!ref?.kind) return trackById(ref?.id);
  if (!['return', 'master'].includes(ref.kind)) return null;
  return auxTracks().find((t) => t.id === ref.id && t.kind === ref.kind);
};
const deviceTrackFromArg = (value) => {
  if (value === 'master') return song.master_track;
  const match = /^return:(\d+)$/.exec(value || '');
  if (match) return song.return_tracks[Number(match[1])];
  return song.tracks[Number(value) || 0];
};
const mixParamAllowed = (track, param, index) => {
  if (track?.kind === 'master') {
    return index == null && ['volume', 'panning', 'crossfader', 'cue_volume'].includes(param);
  }
  if (track?.kind === 'return') return index == null && ['volume', 'panning'].includes(param);
  if (param === 'send') return Number.isInteger(index) && index >= 0;
  return index == null && ['volume', 'panning'].includes(param);
};
const toggleAllowed = (track, param) => {
  if (track?.kind === 'master') return false;
  if (track?.kind === 'return') return ['mute', 'solo'].includes(param);
  return ['mute', 'solo', 'arm'].includes(param);
};
const sceneIdx = (id) => song.scenes.findIndex((s) => s.id === id);
const trackRef = (t) => ({ id: t.id, name: t.name });
const sceneRef = (s) => ({ id: s.id });
const deviceSignature = (d) => `${d.class_name}\u0000${d.class_display_name}`;
const deviceRef = (container, device) => ({
  class_name: device.class_name,
  class_display_name: device.class_display_name,
  ordinal: container.devices.slice(0, container.devices.indexOf(device))
    .filter((candidate) => deviceSignature(candidate) === deviceSignature(device)).length,
});
const parameterRef = (device, parameter) => ({
  name: parameter.name,
  ordinal: device.parameters.slice(0, device.parameters.indexOf(parameter))
    .filter((candidate) => candidate.name === parameter.name).length,
});
const chainGroups = (rack) => [
  ['chains', rack.chains || []],
  ['return_chains', rack.return_chains || []],
];
const chainLocator = (track, parentId, container, rack, kind, idx, chain) => {
  const locator = {
    track: track.id,
    parent_chain: parentId,
    rack: deviceRef(container, rack),
    kind,
    idx,
    name: chain.name,
  };
  if (track.kind) locator.track_kind = track.kind;
  return locator;
};
const auxLocator = (track, idx) => track.kind === 'master'
  ? { kind: 'master' }
  : { kind: 'return', idx, name: track.name };
let auxTrackRecords = [];
function refreshAuxTrackIds(preferredRecords = [], randomFallback = false) {
  const preferred = new Map(preferredRecords.map((rec) => {
    const locator = rec.kind === 'master'
      ? { kind: 'master' }
      : { kind: 'return', idx: rec.idx, name: rec.name };
    return [JSON.stringify(locator), rec.id];
  }));
  auxTrackRecords = [];
  auxTracks().forEach((track) => {
    const idx = track.kind === 'return' ? song.return_tracks.indexOf(track) : 0;
    const locator = auxLocator(track, idx);
    const key = JSON.stringify(locator);
    track.id = preferred.get(key) || track.id || (randomFallback
      ? newId()
      : createHash('sha256').update(key).digest('hex').slice(0, 12));
    auxTrackRecords.push({ id: track.id, kind: track.kind, idx, name: track.name });
  });
}
let chainRecords = [];
function refreshChainIds(preferredRecords = []) {
  const preferred = new Map(preferredRecords.map((rec) => {
    const { id: _id, ...locator } = rec;
    return [JSON.stringify(locator), rec.id];
  }));
  chainRecords = [];
  const walk = (track, container, parentId, depth) => {
    if (depth > 16) return;
    for (const rack of container.devices || []) {
      for (const [kind, chains] of chainGroups(rack)) {
        chains.forEach((chain, idx) => {
          const locator = chainLocator(track, parentId, container, rack, kind, idx, chain);
          const key = JSON.stringify(locator);
          chain.id = preferred.get(key) || chain.id || createHash('sha256').update(key).digest('hex').slice(0, 12);
          chainRecords.push({ ...locator, id: chain.id });
          walk(track, chain, chain.id, depth + 1);
        });
      }
    }
  };
  for (const track of allDeviceTracks()) walk(track, track, null, 0);
}
const chainInContainer = (container, id) => {
  for (const rack of container.devices || []) {
    for (const [_kind, chains] of chainGroups(rack)) {
      const chain = chains.find((candidate) => candidate.id === id);
      if (chain) return chain;
    }
  }
  return null;
};
const resolveDeviceParameter = (track, chainPath, dref, pref) => {
  let container = track;
  for (const cref of chainPath || []) {
    container = chainInContainer(container, cref?.id);
    if (!container) return {};
  }
  const devices = container.devices.filter((device) =>
    device.class_name === dref?.class_name && device.class_display_name === dref?.class_display_name);
  const device = devices[dref?.ordinal];
  const parameters = device?.parameters.filter((parameter) => parameter.name === pref?.name) || [];
  return { device, parameter: parameters[pref?.ordinal] };
};
const locateDevice = (track, path) => {
  let container = track;
  const chainPath = [];
  for (let i = 0; i < path.length; i += 2) {
    const device = container?.devices[path[i]];
    if (!device) return {};
    if (i === path.length - 1) return { container, device, chainPath };
    const chain = device.chains?.[path[i + 1]];
    if (!chain) return {};
    chainPath.push({ id: chain.id });
    container = chain;
  }
  return {};
};
const noteRegion = (note) => {
  const fromPitch = Math.floor(note.pitch / NOTE_PITCH_SPAN) * NOTE_PITCH_SPAN;
  const fromTime = Math.floor(note.start_time / NOTE_TIME_SPAN) * NOTE_TIME_SPAN;
  return {
    from_pitch: fromPitch,
    pitch_span: Math.min(NOTE_PITCH_SPAN, 128 - fromPitch),
    from_time: fromTime,
    time_span: NOTE_TIME_SPAN,
  };
};
const noteInRegion = (note, region) =>
  note.pitch >= region.from_pitch && note.pitch < region.from_pitch + region.pitch_span &&
  note.start_time >= region.from_time && note.start_time < region.from_time + region.time_span;
const clipPayload = (t, s, clip) => ({
  track: trackRef(t),
  scene: sceneRef(s),
  clip: { length: clip.length, name: clip.name },
});

const udp = createSocket('udp4');
const send = (m) => udp.send(Buffer.from(JSON.stringify(m)), PORT_DAEMON, '127.0.0.1');

// --script і --events дозволяють вдати старіший bridge і перевірити,
// що розсинхрон версій виявляється при конекті
const sendHello = () =>
  send({
    m: 'hello',
    live: arg('live', 'fake-12.3.8'),
    script: arg('script', '0.16.0-fake'),
    pid: process.pid,
    features: ['apply_ack'],
    events: arg('events',
      'TransportSet,TempoSet,ClipLaunch,ClipStop,SceneLaunch,StopAllClips,' +
      'TrackCreate,TrackDelete,SceneCreate,SceneDelete,MixerSet,TrackToggle,DeviceParamSet,' +
      'ClipCreate,ClipDelete,ClipNotesSet').split(','),
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
  refreshAuxTrackIds([], true);
  refreshChainIds();
  registryReady = true;
  console.log('реєстр створено');
  return {
    tracks: song.tracks.map((t, idx) => ({ id: t.id, idx, name: t.name })),
    scenes: song.scenes.map((s, idx) => ({ id: s.id, idx, name: s.name })),
    aux_tracks: auxTrackRecords,
    chains: chainRecords,
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
  refreshAuxTrackIds(reg.aux_tracks || []);
  const knownAux = new Set(auxTrackRecords.map((rec) => rec.id));
  for (const rec of reg.aux_tracks || []) {
    if (!knownAux.has(rec.id)) problems.push(`aux track ${rec.kind}/${rec.name}: unresolved`);
  }
  refreshChainIds(reg.chains || []);
  const knownChains = new Set(chainRecords.map((rec) => rec.id));
  for (const rec of reg.chains || []) {
    if (!knownChains.has(rec.id)) problems.push(`Rack chain ${rec.name}: не зіставився`);
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
      const t = deviceTrackByRef(payload.track);
      if (!t) return reject('невідомий трек');
      const index = payload.index ?? null;
      if (!mixParamAllowed(t, payload.param, index)) return reject('недопустимий параметр mixer');
      t.mix[`${payload.param}:${index ?? '-'}`] = payload.value;
      break;
    }
    case 'DeviceParamSet': {
      const t = deviceTrackByRef(payload.track);
      if (!t) return reject('невідомий трек');
      const { device, parameter } = resolveDeviceParameter(
        t, payload.chain_path || [], payload.device, payload.parameter);
      if (!device) return reject('невідомий device');
      if (!parameter) return reject('невідомий параметр device');
      const value = Number(payload.value);
      if (!Number.isFinite(value)) return reject('некоректне значення');
      parameter.value = Math.max(parameter.min, Math.min(parameter.max, value));
      break;
    }
    case 'TrackToggle': {
      const t = deviceTrackByRef(payload.track);
      if (!t) return reject('невідомий трек');
      if (!toggleAllowed(t, payload.param)) return reject('недопустимий перемикач');
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
        clips: emptyClips(song.scenes.length),
        devices: [],
        mix: {},
        mute: false,
        solo: false,
        arm: false,
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
      for (const t of song.tracks) {
        t.clips.splice(idx, 0, null);
        t.slots = song.scenes.length;
      }
      break;
    }
    case 'SceneDelete': {
      const i = sceneIdx(payload.scene?.id);
      if (i < 0) return reject('сцена вже видалена');
      song.scenes.splice(i, 1);
      for (const t of song.tracks) {
        t.clips.splice(i, 1);
        t.slots = song.scenes.length;
      }
      break;
    }
    case 'ClipCreate': {
      const t = trackById(payload.track?.id);
      const s = sceneIdx(payload.scene?.id);
      if (!t) return reject('невідомий трек');
      if (s < 0) return reject('невідома сцена');
      if (t.clips[s]?.kind === 'audio') return reject('у слоті audio clip');
      if (!t.clips[s]) {
        t.clips[s] = {
          kind: 'midi',
          length: payload.clip?.length || NOTE_TIME_SPAN,
          name: payload.clip?.name || '',
          notes: [],
        };
      }
      break;
    }
    case 'ClipDelete': {
      const t = trackById(payload.track?.id);
      const s = sceneIdx(payload.scene?.id);
      if (!t) return reject('невідомий трек');
      if (s < 0) return reject('невідома сцена');
      t.clips[s] = null;
      break;
    }
    case 'ClipNotesSet': {
      const t = trackById(payload.track?.id);
      const s = sceneIdx(payload.scene?.id);
      if (!t) return reject('невідомий трек');
      if (s < 0) return reject('невідома сцена');
      if (t.clips[s]?.kind === 'audio') return reject('у слоті audio clip');
      const clip = t.clips[s] ||= {
        kind: 'midi',
        length: payload.clip?.length || NOTE_TIME_SPAN,
        name: payload.clip?.name || '',
        notes: [],
      };
      clip.notes = clip.notes.filter((note) => !noteInRegion(note, payload.region));
      clip.notes.push(...(payload.notes || []).map((note) => ({ ...note })));
      clip.notes.sort((a, b) => a.start_time - b.start_time || a.pitch - b.pitch);
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
      const t = { id: newId(), name: `${idx + 1}-${kind === 'midi' ? 'MIDI' : 'Audio'}`, playing_slot_index: -1, slots: song.scenes.length, clips: emptyClips(song.scenes.length), devices: [], mix: {}, mute: false, solo: false, arm: false };
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
      for (const t of song.tracks) {
        t.clips.splice(idx, 0, null);
        t.slots = song.scenes.length;
      }
      emit('SceneCreate', { scene: { id: s.id }, idx });
      break;
    }
    case 'delscene': {
      const s = song.scenes[Number(rest[0]) || 0];
      if (!s) return console.log('немає такої сцени');
      const idx = song.scenes.indexOf(s);
      song.scenes.splice(idx, 1);
      for (const t of song.tracks) {
        t.clips.splice(idx, 1);
        t.slots = song.scenes.length;
      }
      emit('SceneDelete', { scene: { id: s.id } });
      break;
    }
    case 'note': {
      const t = track();
      const s = song.scenes[Number(rest[1]) || 0];
      const pitch = Number(rest[2]);
      const start = Number(rest[3]);
      const duration = Number(rest[4]);
      const velocity = Number(rest[5] ?? 100);
      if (!t || !s || !Number.isInteger(pitch) || pitch < 0 || pitch > 127 ||
          !Number.isFinite(start) || !Number.isFinite(duration) || duration <= 0) {
        return console.log('некоректна нота або адреса кліпу');
      }
      const sidx = song.scenes.indexOf(s);
      let clip = t.clips[sidx];
      if (!clip) {
        clip = t.clips[sidx] = { kind: 'midi', length: Math.max(NOTE_TIME_SPAN, start + duration), name: '', notes: [] };
        emit('ClipCreate', clipPayload(t, s, clip));
      }
      if (clip.kind !== 'midi') return console.log('у слоті audio clip');
      clip.length = Math.max(clip.length, start + duration);
      const note = {
        pitch,
        start_time: start,
        duration,
        velocity,
        mute: false,
        probability: 1,
        velocity_deviation: 0,
        release_velocity: 64,
      };
      clip.notes.push(note);
      const region = noteRegion(note);
      emit('ClipNotesSet', {
        ...clipPayload(t, s, clip),
        region,
        notes: clip.notes.filter((n) => noteInRegion(n, region)),
      });
      break;
    }
    case 'delnote': {
      const t = track();
      const s = song.scenes[Number(rest[1]) || 0];
      const pitch = Number(rest[2]);
      const start = Number(rest[3]);
      const sidx = song.scenes.indexOf(s);
      const clip = t?.clips[sidx];
      if (!t || !s || clip?.kind !== 'midi') return console.log('немає такого MIDI clip');
      const sample = { pitch, start_time: start };
      const region = noteRegion(sample);
      clip.notes = clip.notes.filter((n) => n.pitch !== pitch || n.start_time !== start);
      emit('ClipNotesSet', {
        ...clipPayload(t, s, clip),
        region,
        notes: clip.notes.filter((n) => noteInRegion(n, region)),
      });
      break;
    }
    case 'delclip': {
      const t = track();
      const s = song.scenes[Number(rest[1]) || 0];
      const sidx = song.scenes.indexOf(s);
      if (!t || !s || !t.clips[sidx]) return console.log('немає такого clip');
      t.clips[sidx] = null;
      emit('ClipDelete', { track: trackRef(t), scene: sceneRef(s) });
      break;
    }
    case 'device': {
      const t = deviceTrackFromArg(rest[0]);
      const path = String(rest[1] || '').split('/').map(Number);
      const { container, device, chainPath } = locateDevice(t, path);
      const parameter = device?.parameters[Number(rest[2])];
      const value = Number(rest[3]);
      if (!t || !container || !device || !parameter || !Number.isFinite(value)) {
        return console.log('немає такого device/parameter або значення некоректне');
      }
      parameter.value = Math.max(parameter.min, Math.min(parameter.max, value));
      const payload = {
        track: deviceTrackRef(t),
        device: deviceRef(container, device),
        parameter: parameterRef(device, parameter),
        value: parameter.value,
      };
      if (chainPath.length) payload.chain_path = chainPath;
      emit('DeviceParamSet', payload);
      break;
    }
    case 'mix': {
      const t = deviceTrackFromArg(rest[0]);
      const param = rest[1];
      const value = Number(rest[2]);
      const idx = rest[3] == null ? null : Number(rest[3]);
      if (!t || !Number.isFinite(value) || !mixParamAllowed(t, param, idx)) {
        return console.log('немає такого mixer parameter або значення некоректне');
      }
      t.mix[`${param}:${idx ?? '-'}`] = value;
      const payload = { track: deviceTrackRef(t), param, value };
      if (idx !== null) payload.index = idx;
      emit('MixerSet', payload);
      break;
    }
    case 'toggle': {
      const t = deviceTrackFromArg(rest[0]);
      const param = rest[1];
      if (!t || !toggleAllowed(t, param)) {
        return console.log('немає такого mixer toggle');
      }
      t[param] = !t[param];
      emit('TrackToggle', { track: deviceTrackRef(t), param, value: t[param] });
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
        'note <t> <s> <pitch> <start> <duration> [velocity] | delnote <t> <s> <pitch> <start> | delclip <t> <s>',
        'device <track> <device[/chain/device...]> <parameter> <value> | vol <t> <value> | pan <t> <value> | send <t> <index> <value>',
        'addtrack [midi|audio] [idx] | deltrack <t> | addscene [idx] | delscene <n>',
        'rename <t> <name> | move <from> <to> | state',
      ].join('\n'));
  }
});
