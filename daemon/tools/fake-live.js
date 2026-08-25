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
import { readFileSync, statSync } from 'node:fs';
import { join as joinPath } from 'node:path';
import { noteRegionsFor, stateToOps } from './state-ops.js';

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const PORT_DAEMON = Number(arg('udp-in', 19845)); // куди шлемо
const PORT_SELF = Number(arg('udp-out', 19846)); // де слухаємо
const NOTE_TIME_SPAN = 4;
// Дзеркало CLIP_LENGTH_MAX із bridge: партнер на старому скрипті шле довжину
// кліпу, який ще писався -- 63072000 доль, тобто заглушку Live на два роки.
const CLIP_LENGTH_MAX = 1e6;
const KNOWN_TRACK_KINDS = new Set(['midi', 'audio']);
const saneLength = (value) => {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 && n <= CLIP_LENGTH_MAX ? n : NOTE_TIME_SPAN;
};
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
// Скалярні властивості пісні. Метроном і midi_recording_quantization
// сюди не входять навмисно -- це особисті налаштування, не документ.
const SONG_PROPS = {
  signature_numerator: (v) => (Number.isInteger(v) && v >= 1 && v <= 99 ? v : null),
  signature_denominator: (v) => ([1, 2, 4, 8, 16].includes(v) ? v : null),
  clip_trigger_quantization: (v) => (Number.isInteger(v) && v >= 0 && v <= 13 ? v : null),
  root_note: (v) => (Number.isInteger(v) && v >= 0 && v <= 11 ? v : null),
  scale_name: (v) => (typeof v === 'string' && v && v.length <= 64 ? v : null),
  scale_mode: (v) => Boolean(v),
  // Петля Arrangement -- "де ми зараз працюємо", найспільніше, що є.
  // punch іде з нею: він змінює те, що ця петля означає для запису.
  loop: (v) => Boolean(v),
  punch_in: (v) => Boolean(v),
  punch_out: (v) => Boolean(v),
  loop_start: (v) => (Number.isFinite(v) && v >= 0 && v <= 1e6 ? Number(v) : null),
  loop_length: (v) => (Number.isFinite(v) && v > 0 && v <= 1e6 ? Number(v) : null),
};

const song = {
  playing: false,
  tempo: 120,
  // Локатори адресуються часом: CuePoint.time лише на читання, а два
  // локатори не бувають на одній позиції.
  cues: {},
  props: { signature_numerator: 4, signature_denominator: 4,
           clip_trigger_quantization: 4, root_note: 0,
           scale_name: 'Major', scale_mode: false,
           loop: false, loop_start: 0, loop_length: 16,
           punch_in: false, punch_out: false },
  tracks: [
    { id: null, name: '1-MIDI', color: 0xff8c00, playing_slot_index: -1, slots: 8, clips: emptyClips(8), devices: fakeDevices(), mix: {}, mute: false, solo: false, arm: false },
    { id: null, name: '2-MIDI', color: 0x33aa55, playing_slot_index: -1, slots: 8, clips: emptyClips(8), devices: fakeDevices(), mix: {}, mute: false, solo: false, arm: false },
    { id: null, name: '3-Audio', color: 0x3388dd, playing_slot_index: -1, slots: 8, clips: emptyClips(8), devices: fakeDevices(), mix: {}, mute: false, solo: false, arm: false },
  ],
  return_tracks: [
    { id: null, kind: 'return', name: 'A-Return', color: 0xaa55dd, devices: [fakeFilter(0.2), fakeRack()], mix: {}, mute: false, solo: false },
    { id: null, kind: 'return', name: 'B-Return', color: 0xdd5577, devices: [fakeFilter(0.3)], mix: {}, mute: false, solo: false },
  ],
  view: { track: null, scene: null, screen: 'session' },
  master_track: { id: null, kind: 'master', name: 'Master', color: 0x777777, devices: [fakeFilter(0.7), fakeRack()], mix: {} },
  scenes: [0, 1, 2, 3, 4, 5, 6, 7].map((i) => ({ id: null, name: `Scene ${i + 1}`, color: 0x444444 + i })),
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
const metadataTarget = (payload) => {
  if (payload.object === 'track') return deviceTrackByRef(payload.track);
  if (payload.object === 'scene') return song.scenes.find((s) => s.id === payload.scene?.id);
  if (payload.object === 'clip') {
    const t = trackById(payload.track?.id);
    const sidx = sceneIdx(payload.scene?.id);
    return t && sidx >= 0 ? t.clips[sidx] : null;
  }
  return null;
};
const metadataFromArg = (value) => {
  if (value === 'master') return { object: 'track', target: song.master_track };
  let match = /^return:(\d+)$/.exec(value || '');
  if (match) return { object: 'track', target: song.return_tracks[Number(match[1])] };
  match = /^scene:(\d+)$/.exec(value || '');
  if (match) return { object: 'scene', target: song.scenes[Number(match[1])] };
  match = /^clip:(\d+):(\d+)$/.exec(value || '');
  if (match) {
    const track = song.tracks[Number(match[1])];
    const scene = song.scenes[Number(match[2])];
    return { object: 'clip', target: track?.clips[Number(match[2])], track, scene };
  }
  match = /^(?:track:)?(\d+)$/.exec(value || '');
  return match ? { object: 'track', target: song.tracks[Number(match[1])] } : null;
};
const metadataAddress = ({ object, target, track, scene }) => {
  if (object === 'track') return { object, track: deviceTrackRef(target) };
  if (object === 'scene') return { object, scene: sceneRef(target) };
  if (object === 'clip') return { object, track: trackRef(track), scene: sceneRef(scene) };
  return null;
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
  clip: { length: clip.length, name: clip.name, color: clip.color },
});

const udp = createSocket('udp4');
const send = (m) => udp.send(Buffer.from(JSON.stringify(m)), PORT_DAEMON, '127.0.0.1');

// --script і --events дозволяють вдати старіший bridge і перевірити,
// що розсинхрон версій виявляється при конекті
const sendHello = () =>
  send({
    m: 'hello',
    live: arg('live', 'fake-12.3.8'),
    script: arg('script', '0.19.0-dev-fake'),
    pid: process.pid,
    features: ['apply_ack', 'full_state', 'state_apply', 'presence', 'view_follow'],
    events: arg('events',
      'TransportSet,TempoSet,ClipLaunch,ClipStop,SceneLaunch,StopAllClips,' +
      'TrackCreate,TrackDelete,TrackDuplicate,SceneCreate,SceneDelete,MixerSet,TrackToggle,DeviceParamSet,ObjectMetaSet,' +
      'ClipCreate,ClipDelete,ClipNotesSet,ClipLoopSet,DeviceLoad,' +
      'DeviceInsert,DeviceDelete,DeviceMove,SampleLoad,SongPropSet,SceneTimingSet,ClipPropSet,CueSet,CueDelete,' +
      'ArrangementClipCreate,ArrangementClipMove,ArrangementClipDelete,ArrangementClipNotesSet').split(','),
  });

function emit(type, payload) {
  if (!registryReady && type !== 'TransportSet' && type !== 'TempoSet') {
    return console.log('реєстр ще не готовий — подію не відправлено');
  }
  lseq += 1;
  send({ m: 'event', type, payload, lseq });
  console.log(`-> ${type} ${JSON.stringify(payload)}`);
}

// Live віддає -1 замість значення, коли перевизначення вимкнене, тож поля
// взаємозалежні й їдуть одним блоком.
const sceneTiming = (block) => {
  if (!block) return null;
  const out = { tempo_enabled: Boolean(block.tempo_enabled),
                time_signature_enabled: Boolean(block.time_signature_enabled) };
  if (out.tempo_enabled) {
    const t = Number(block.tempo);
    if (!(t >= 20 && t <= 999)) return null;
    out.tempo = t;
  }
  if (out.time_signature_enabled) {
    const n = Number(block.time_signature_numerator);
    const d = Number(block.time_signature_denominator);
    if (!(Number.isInteger(n) && n >= 1 && n <= 99)) return null;
    if (![1, 2, 4, 8, 16].includes(d)) return null;
    out.time_signature_numerator = n;
    out.time_signature_denominator = d;
  }
  return out;
};

// Властивості кліпу поза межами. Частина існує лише в audio -- на MIDI
// їх просто немає, і це різниця типів, а не помилка.
const CLIP_PROPS = {
  gain: (v) => (v >= 0 && v <= 1 ? Number(v) : null),
  velocity_amount: (v) => (v >= 0 && v <= 1 ? Number(v) : null),
  pitch_coarse: (v) => (Number.isInteger(v) && v >= -48 && v <= 48 ? v : null),
  pitch_fine: (v) => (Number.isInteger(v) && v >= -50 && v <= 50 ? v : null),
  warp_mode: (v) => (Number.isInteger(v) && v >= 0 && v <= 6 ? v : null),
  launch_mode: (v) => (Number.isInteger(v) && v >= 0 && v <= 4 ? v : null),
  launch_quantization: (v) => (Number.isInteger(v) && v >= 0 && v <= 13 ? v : null),
  warping: (v) => Boolean(v),
  muted: (v) => Boolean(v),
  legato: (v) => Boolean(v),
  ram_mode: (v) => Boolean(v),
};

const snapshot = () => ({
  playing: song.playing,
  tempo: song.tempo,
  song: { ...song.props },
  cues: Object.entries(song.cues).map(([time, name]) => ({ time: Number(time), name })),
  tracks: song.tracks.map((t, idx) => ({ ...t, idx })),
  scenes: song.scenes.map((s, idx) => ({ ...s, idx })),
});

// ------------------------------------------------------- повний стан (state)
//
// Дзеркало _full_state() з Remote Script: та сама форма й ті самі адреси, щоб
// серіалізатор можна було ганяти без Live.
const STATE_CHUNK_CHARS = 30000;
const STATE_CHUNKS_PER_TICK = 6;
let stateQueue = [];
let stateId = 0;

const mixerState = (track) => {
  const mixer = {};
  for (const [key, value] of Object.entries(track.mix || {})) {
    const [param, idx] = key.split(':');
    if (param === 'send') {
      mixer.sends = mixer.sends || [];
      mixer.sends.push({ index: Number(idx), value });
    } else {
      mixer[param] = value;
    }
  }
  for (const prop of ['mute', 'solo', 'arm']) {
    if (prop in track) mixer[prop] = !!track[prop];
  }
  return mixer;
};

const deviceEntries = (track) => {
  const out = [];
  const walk = (container, chainPath, depth) => {
    if (depth > 16) return;
    const ordinals = new Map();
    for (const device of container.devices || []) {
      const signature = `${device.class_name}|${device.class_display_name}`;
      const ordinal = ordinals.get(signature) || 0;
      ordinals.set(signature, ordinal + 1);
      const nameOrdinals = new Map();
      const parameters = (device.parameters || []).map((parameter) => {
        const pordinal = nameOrdinals.get(parameter.name) || 0;
        nameOrdinals.set(parameter.name, pordinal + 1);
        return { name: parameter.name, ordinal: pordinal, value: parameter.value };
      });
      const entry = {
        device: {
          class_name: device.class_name,
          class_display_name: device.class_display_name,
          ordinal,
        },
        parameters,
      };
      if (chainPath.length) entry.chain_path = chainPath;
      out.push(entry);
      for (const [, chains] of chainGroups(device)) {
        for (const chain of chains) {
          if (chain.id) walk(chain, [...chainPath, { id: chain.id }], depth + 1);
        }
      }
    }
  };
  walk(track, [], 0);
  return out;
};

const clipsState = (track) => (track.clips || []).map((clip, idx) => {
  const scene = song.scenes[idx];
  if (!clip || !scene?.id) return null;
  const entry = {
    scene: { id: scene.id },
    clip: { length: clip.length, name: clip.name, color: clip.color },
  };
  if (clip.kind === 'midi') entry.notes = clip.notes;
  const loop = {};
  for (const prop of ['looping', 'loop_start', 'loop_end', 'start_marker', 'end_marker']) {
    if (clip[prop] !== undefined) loop[prop] = clip[prop];
  }
  if (Object.keys(loop).length) entry.loop = loop;
  return entry;
}).filter(Boolean);

// ------------------------------------------------------- Arrangement (мірror)
//
// Кліп в Arrangement не може носити власний id (Clip.set_data не існує),
// тож у bridge ідентичність живе в мапі на Song. Тут ми тримаємо її просто
// в моделі -- нам важлива не персистенція, а те, що uuid є і він стабільний.
// Подій для Arrangement поки немає: стадія A -- лише знімок і звіт.

const arrOf = (t) => (t.arrangement || (t.arrangement = []));

const arrState = (t) => arrOf(t).map((c) => ({
  id: c.id,
  start_time: c.start_time,
  end_time: c.start_time + c.length,
  length: c.length,
  name: c.name,
  color: c.color,
  is_midi: true,
}));

// Емісія подій Arrangement. Дзеркало _diff_arrangement: переїзд розпізнається
// за тим, що вижив сам обʼєкт (тут -- його uuid), а не за схожістю кліпів.
let arrSnapshot = new Map();

const primeArrangement = () => {
  arrSnapshot = new Map();
  for (const t of song.tracks) {
    if (!t.id) continue;
    for (const c of arrOf(t)) arrSnapshot.set(c.id, { track: t.id, start: c.start_time });
  }
};

const diffArrangement = () => {
  const seen = new Set();
  for (const t of song.tracks) {
    if (!t.id) continue;
    for (const c of arrOf(t)) {
      seen.add(c.id);
      const was = arrSnapshot.get(c.id);
      if (was) {
        if (was.start !== c.start_time) {
          emit('ArrangementClipMove', { track: { id: t.id }, clip: { id: c.id }, start_time: c.start_time });
        }
        continue;
      }
      if (c.file_path) {
        // Партнер не створить audio-кліп із нічого, зате завантажить
        // той самий файл -- тож структурної події тут не шлемо взагалі.
        emit('SampleLoad', {
          track: { id: t.id },
          clip: { id: c.id },
          target: { kind: 'arrangement', start_time: c.start_time },
          sample: { path: c.file_path, name: String(c.file_path).split('/').pop() },
        });
        continue;
      }
      emit('ArrangementClipCreate', {
        track: { id: t.id },
        clip: { id: c.id, length: c.length, name: c.name, color: c.color, is_midi: true },
        start_time: c.start_time,
      });
      // Вміст -- окремими подіями, щоб створення лишалось маленьким
      for (const [region, part] of noteRegionsFor({ length: c.length }, c.notes || [])) {
        if (!part.length) continue;
        emit('ArrangementClipNotesSet', { track: { id: t.id }, clip: { id: c.id }, region, notes: part });
      }
    }
  }
  for (const [id, was] of arrSnapshot) {
    if (!seen.has(id)) emit('ArrangementClipDelete', { track: { id: was.track }, clip: { id } });
  }
};

// Дзеркало _on_arrangement: глушіння те саме, що всюди -- застосування чужої
// події не має повернутись назад власною емісією.
const onArrangement = (suppressStruct) => {
  if (!suppressStruct) diffArrangement();
  primeArrangement();
};

const arrClipById = (id) => {
  for (const t of song.tracks) {
    const clip = arrOf(t).find((c) => c.id === id);
    if (clip) return { track: t, clip };
  }
  return null;
};

/** Розбіжності, які подіями не лікуються. Дзеркало _structural_gaps. */
const structuralGaps = (state) => {
  const gaps = [];
  for (const entry of state.tracks || []) {
    const local = trackById(entry.id);
    if (!local) continue;

    if (Boolean(entry.group) !== Boolean(local.group)) {
      gaps.push({
        what: 'group', track: entry.name || local.name,
        name: (entry.group || local.group || {}).name, here: Boolean(local.group),
      });
    }

    const theirs = new Map((entry.arrangement || []).filter((c) => c.id).map((c) => [c.id, c]));
    const mine = new Map(arrState(local).map((c) => [c.id, c]));
    const name = entry.name || local.name;
    for (const [id, c] of theirs) {
      if (!mine.has(id)) gaps.push({ what: 'arrangement', track: name, here: false, start: c.start_time, name: c.name });
    }
    for (const [id, c] of mine) {
      if (!theirs.has(id)) gaps.push({ what: 'arrangement', track: name, here: true, start: c.start_time, name: c.name });
    }
    for (const [id, c] of mine) {
      const other = theirs.get(id);
      // Той самий кліп на різних позиціях -- переїзд, якого ми не бачили
      if (other && other.start_time !== c.start_time) {
        gaps.push({ what: 'arrangement', track: name, here: null, start: other.start_time, mine: c.start_time, name: c.name });
      }
    }
  }
  return gaps;
};

// ----------------------------------------------------------------- семпли
//
// Портативна адреса -- шлях відносно теки проєкту. Байти возить filesync
// окремо від події, тож подія цілком може приїхати раніше за файл: у bridge
// на цей випадок черга з очікуванням, тут -- чесна відмова.

const projectRoot = arg('project', '');

const sampleExists = (rel) => {
  if (!projectRoot || !rel || rel.includes('..')) return false;
  try {
    return statSync(joinPath(projectRoot, ...String(rel).split('/'))).isFile();
  } catch {
    return false;
  }
};

// Пади Drum Rack. У справжньому Live семпл на паді народжує новий ланцюг
// усередині рака, і дифф девайсів його навмисно пропускає -- інакше партнер
// дістав би голий Simpler без звуку. Тому пади мають власний шлях.
const padsOf = (device) => (device.drum_pads || (device.drum_pads = {}));
const isDrumRack = (device) => device.class_name === 'DrumGroupDevice';

const findDeviceByRef = (container, ref) => {
  if (!ref) return null;
  let ordinal = 0;
  for (const device of container.devices || []) {
    if (device.class_name === ref.class_name &&
        device.class_display_name === ref.class_display_name) {
      if (ordinal === (ref.ordinal || 0)) return device;
      ordinal += 1;
    }
  }
  return null;
};

const fullState = () => ({
  version: 1,
  script: arg('script', '0.19.0-dev-fake'),
  live: arg('live', 'fake-12.3.8'),
  at: Date.now() / 1000,
  tempo: song.tempo,
  playing: song.playing,
  song: { ...song.props },
  cues: Object.entries(song.cues).map(([time, name]) => ({ time: Number(time), name })),
  tracks: song.tracks.filter((t) => t.id).map((t, idx) => ({
    id: t.id,
    idx,
    name: t.name,
    color: t.color,
    kind: t.kind || (/MIDI/i.test(t.name) ? 'midi' : 'audio'),
    group: t.group || null,
    mixer: mixerState(t),
    devices: deviceEntries(t),
    clips: clipsState(t),
    arrangement: arrState(t),
  })),
  aux_tracks: auxTracks().filter((t) => t.id).map((t) => ({
    id: t.id,
    kind: t.kind,
    idx: t.kind === 'return' ? song.return_tracks.indexOf(t) : 0,
    name: t.name,
    color: t.color,
    mixer: mixerState(t),
    devices: deviceEntries(t),
  })),
  scenes: song.scenes.filter((s) => s.id).map((s, idx) => ({
    id: s.id, idx, name: s.name, color: s.color, timing: s.timing || null,
  })),
});

function queueState(requestId) {
  const blob = JSON.stringify(fullState());
  stateId += 1;
  const id = requestId ?? stateId;
  const chunks = [];
  for (let i = 0; i < blob.length; i += STATE_CHUNK_CHARS) {
    chunks.push(blob.slice(i, i + STATE_CHUNK_CHARS));
  }
  if (!chunks.length) chunks.push('');
  stateQueue = chunks.map((data, seq) => ({
    m: 'state_chunk', id, seq, total: chunks.length, chars: blob.length, data,
  }));
  console.log(`state: ${blob.length} символів у ${chunks.length} чанках (id=${id})`);
}

// Порціями по тіках, як у bridge: залп датаграм переповнив би приймальний буфер.
setInterval(() => {
  for (let i = 0; i < STATE_CHUNKS_PER_TICK && stateQueue.length; i += 1) {
    send(stateQueue.shift());
  }
}, 100).unref();


// Дзеркало _op_gap: apply на нерозвʼязану адресу мовчки виходить, тож без
// окремої перевірки звіт рахував би пропущене як застосоване.
const MISSING_LIMIT = 50;

function opGap(type, payload) {
  if (['TempoSet', 'TransportSet', 'StopAllClips'].includes(type)) return null;

  let track = null;
  if (payload.track?.id) {
    track = deviceTrackByRef(payload.track);
    if (!track) return { what: 'track', id: payload.track.id, kind: payload.track.kind };
  }

  if (type === 'DeviceLoad') {
    const item = browserItem(payload.item);
    if (!item) {
      return { what: 'device_item', name: payload.item?.name, uri: payload.item?.uri };
    }
    return null;
  }

  if (type === 'DeviceParamSet') {
    const { device, parameter } = resolveDeviceParameter(
      track, payload.chain_path, payload.device, payload.parameter);
    const display = payload.device?.class_display_name;
    if (!device) return { what: 'device', track: track?.name, device: display };
    if (!parameter) {
      return { what: 'parameter', track: track?.name, device: display, name: payload.parameter?.name };
    }
    return null;
  }

  if (payload.scene?.id) {
    const sidx = sceneIdx(payload.scene.id);
    if (sidx < 0) return { what: 'scene', id: payload.scene.id };
    if (['ClipCreate', 'ClipNotesSet', 'ClipLoopSet'].includes(type) || payload.object === 'clip') {
      if (!track || sidx >= track.clips.length) {
        return { what: 'clip', track: track?.name, scene: payload.scene.id };
      }
    }
  }
  return null;
}

function startStateApply(path, id) {
  let state;
  try {
    state = JSON.parse(readFileSync(path, 'utf8'));
  } catch (error) {
    return console.log(`state apply: не читається ${path}: ${error.message}`);
  }
  const ops = stateToOps(state);
  const report = { m: 'state_applied', id, total: ops.length, ok: 0, skipped: 0, failed: 0, errors: [] };
  const missing = new Map();
  let missingMore = 0;
  for (const [type, payload] of ops) {
    const gap = opGap(type, payload);
    if (gap) {
      report.skipped += 1;
      const key = JSON.stringify(gap);
      if (missing.has(key)) missing.get(key).count += 1;
      else if (missing.size < MISSING_LIMIT) missing.set(key, { ...gap, count: 1 });
      else missingMore += 1;
      continue;
    }
    try {
      apply(type, payload, 'state');
      report.ok += 1;
    } catch (error) {
      report.failed += 1;
      if (report.errors.length < 20) report.errors.push(`${type}: ${error.message}`);
    }
  }
  // Структурні розбіжності -- не пропущені операції, тож лічильники
  // ok/skipped їх не рахують: це "у нас різна розкладка", а не збій.
  const structural = structuralGaps(state);
  report.missing = [...missing.values()].sort((a, b) => b.count - a.count).concat(structural);
  report.missing_more = missingMore;
  console.log(`state apply: ${report.ok} з ${report.total} (${report.skipped} пропущено, ${report.failed} помилок)`);
  send(report);
}


// ------------------------------------------------------------- вид (присутність)
//
// Дзеркало view-частини Remote Script: віддаємо, на що дивимось, і вміємо
// поставити чужий вид. Головне тут -- застосування чужого виду НЕ породжує
// власного повідомлення: саме це і перевіряє E2E на ping-pong.
const viewPayload = () => {
  const track = song.view.track;
  const scene = song.view.scene;
  if (!track?.id && !scene?.id) return null;
  const view = { screen: song.view.screen, names: {} };
  if (track?.id) {
    view.track = track.kind ? { id: track.id, kind: track.kind } : { id: track.id };
    view.names.track = track.name;
  }
  if (scene?.id) {
    view.scene = { id: scene.id };
    view.names.scene = scene.name;
  }
  if (track?.id && scene?.id && !track.kind) {
    const idx = song.scenes.indexOf(scene);
    if (idx >= 0 && track.clips?.[idx]) view.clip = { track: track.id, scene: scene.id };
  }
  return view;
};

function sendView() {
  if (!registryReady) return; // до бутстрапу uuid ще не спільні
  const view = viewPayload();
  console.log(`-> view ${JSON.stringify(view)}`);
  send({ m: 'view', view });
}

function applyViewSet(msg) {
  const view = msg.view || {};
  const track = view.track ? deviceTrackByRef(view.track) : null;
  const sidx = view.scene?.id ? sceneIdx(view.scene.id) : -1;
  if (view.track && !track) console.log(`<- view_set: трек ${view.track.id} невідомий`);
  if (view.scene?.id && sidx < 0) console.log(`<- view_set: сцена ${view.scene.id} невідома`);
  if (track) song.view.track = track;
  if (sidx >= 0) song.view.scene = song.scenes[sidx];
  console.log(`<- view_set від ${msg.from}: ${song.view.track?.name || '—'} / ` +
    `${song.view.scene?.name || '—'}`);
  // Назад нічого не шлемо: це чужий вид, а не наш рух.
}


// Каталог браузера: ті самі uri, що ми зняли з двох живих машин.
// Стокові девайси адресуються назвою і збігаються; вміст (drums) -- ні.
const BROWSER = {
  audio_effects: [
    { uri: 'query:AudioFx#Compressor', name: 'Compressor', class_name: 'Compressor2' },
    { uri: 'query:AudioFx#Auto%20Filter', name: 'Auto Filter', class_name: 'AutoFilter' },
    { uri: 'query:AudioFx#Audio%20Effect%20Rack', name: 'Audio Effect Rack', class_name: 'AudioEffectGroupDevice' },
  ],
  instruments: [
    { uri: 'query:Synths#Drum%20Rack', name: 'Drum Rack', class_name: 'DrumGroupDevice' },
    { uri: 'query:Synths#Operator', name: 'Operator', class_name: 'Operator' },
  ],
  midi_effects: [],
};

const browserItem = (ref) => {
  const list = BROWSER[ref?.category];
  if (!list) return null;
  return list.find((i) => i.uri === ref.uri)
    || list.find((i) => i.name.toLowerCase() === String(ref.name || '').toLowerCase())
    || null;
};

// --- Етап 2 DeviceLoad: автоемісія. Дзеркало _diff_devices із bridge.

const deviceName = (d) => (d.name === undefined ? d.class_display_name : d.name);

// Назва -> [{категорія, айтем}]. З девайса uri не читається взагалі, тож
// єдина ниточка назад у браузер -- відображувана назва.
const browserNamed = () => {
  const named = new Map();
  for (const [category, list] of Object.entries(BROWSER)) {
    for (const item of list) {
      const key = item.name.toLowerCase();
      if (!named.has(key)) named.set(key, []);
      named.get(key).push({ category, item });
    }
  }
  return named;
};

// Пресет має той самий class_name, що й голий девайс: різниця лише в name.
const deviceIsBare = (device) => {
  if (deviceName(device) !== device.class_display_name) return false;
  for (const [, chains] of chainGroups(device)) if (chains.length) return false;
  return true;
};

const deviceBrowserRef = (device) => {
  if (!deviceIsBare(device)) return null;
  const matches = browserNamed().get(String(device.class_display_name).toLowerCase()) || [];
  if (matches.length !== 1) return null;
  const { category, item } = matches[0];
  return { uri: item.uri, name: item.name, category, class_name: device.class_name };
};

// Разом із name: інакше пресет не відрізнити від голого девайса поруч,
// і вставку зарахувало б сусідові.
// Формат -- JSON трійки, а не склеєний рядок: DeviceDelete і DeviceMove несуть
// сигнатуру девайса, якого вже немає, а дістати її можна лише зі знімка.
const treeSig = (d) => JSON.stringify([d.class_name, d.class_display_name, deviceName(d)]);
const sigPayload = (sig) => {
  const [class_name, class_display_name, name] = JSON.parse(sig);
  return { class_name, class_display_name, name };
};

const containerByPath = (trackRef, chainPath) => {
  const t = deviceTrackByRef(trackRef);
  if (!t) return null;
  let container = t;
  for (const cref of chainPath || []) {
    container = chainInContainer(container, cref?.id);
    if (!container) return null;
  }
  return container;
};

// Індекс каже, що поїхало; сигнатура ловить те, що поїхало не те. Без неї
// розбіжність станів стерла б партнеру чужий девайс мовчки.
const deviceMatches = (device, ref) => {
  if (!ref) return true;
  if (ref.class_name && ref.class_name !== device.class_name) return false;
  if (ref.class_display_name && ref.class_display_name !== device.class_display_name) return false;
  if (ref.name !== undefined && deviceName(device) !== ref.name) return false;
  return true;
};

const deviceTree = () => {
  const tree = new Map();
  const walk = (ref, container, chainPath) => {
    tree.set(JSON.stringify([ref, chainPath]), {
      container, ref, chainPath, sigs: (container.devices || []).map(treeSig),
    });
    for (const rack of container.devices || []) {
      for (const [, chains] of chainGroups(rack)) {
        for (const chain of chains) walk(ref, chain, [...chainPath, { id: chain.id }]);
      }
    }
  };
  for (const t of allDeviceTracks()) walk(deviceTrackRef(t), t, []);
  return tree;
};

let deviceTreeSnapshot = new Map();
const primeDevices = () => {
  deviceTreeSnapshot = new Map([...deviceTree()].map(([k, v]) => [k, v.sigs]));
};

// У bridge це змінна оточення: там режим -- рішення розгортання. Тут ще й
// команда, бо інакше перевірка другого режиму коштувала б окремої сесії
// з релеєм, демоном і двома емуляторами заради одного прапорця.
let deviceMoveMode = (process.env.ABLETONMP_DEVICE_MOVE || 'move').toLowerCase();

const singleChangeSpot = (short, long_) => {
  for (let i = 0; i < long_.length; i += 1) {
    if (short.every((s, j) => s === long_[j < i ? j : j + 1])) return i;
  }
  return -1;
};

const withChain = (entry, extra) => (entry.chainPath.length
  ? { ...extra, chain_path: entry.chainPath } : extra);

// Голизна потрібна лише вставці: insert_device уміє тільки стокову назву,
// тож пресет приїхав би партнеру дефолтом. Видалення й переїзд працюють
// за індексом, тому їм байдуже.
const emitDeviceInsert = ({ entry, spot, sig }) => {
  const device = entry.container.devices[spot];
  if (!device || !deviceIsBare(device)) return;
  emit('DeviceInsert', withChain(entry, {
    track: entry.ref, index: spot,
    device: { class_display_name: JSON.parse(sig)[1] },
  }));
};

const emitDeviceDelete = ({ entry, spot, sig }) => {
  emit('DeviceDelete', withChain(entry, {
    track: entry.ref, index: spot, device: sigPayload(sig),
  }));
};

const emitDeviceMove = (gone, appeared) => {
  if (deviceMoveMode === 'pair') {
    // Пара коштує партнеру значень параметрів. А якщо девайс не голий,
    // вставка неможлива, і саме лише видалення знищило б його без заміни.
    const device = appeared.entry.container.devices[appeared.spot];
    if (!device || !deviceIsBare(device)) return;
    emitDeviceDelete(gone);
    emitDeviceInsert(appeared);
    return;
  }
  emit('DeviceMove', {
    from: withChain(gone.entry, { track: gone.entry.ref, index: gone.spot }),
    to: withChain(appeared.entry, { track: appeared.entry.ref, index: appeared.spot }),
    device: sigPayload(gone.sig),
  });
};

// Дзеркало _diff_devices. Одна поява -> Insert, одне зникнення -> Delete,
// зникнення й поява тієї самої сигнатури -> переїзд. Складніше -- мовчимо.
const diffDevices = () => {
  if (!deviceTreeSnapshot.size) return;
  const current = deviceTree();
  const added = [];
  const removed = [];
  for (const [key, entry] of current) {
    const was = deviceTreeSnapshot.get(key);
    if (!was) continue;                       // новий контейнер: копія треку
    const now = entry.sigs;
    if (now.length === was.length && now.every((s, i) => s === was[i])) continue;
    if (now.length === was.length + 1) {
      const spot = singleChangeSpot(was, now);
      if (spot < 0) return;
      added.push({ entry, spot, sig: now[spot] });
    } else if (now.length + 1 === was.length) {
      const spot = singleChangeSpot(now, was);
      if (spot < 0) return;
      removed.push({ entry, spot, sig: was[spot] });
    } else return;
  }
  if (added.length === 1 && removed.length === 1 && added[0].sig === removed[0].sig) {
    emitDeviceMove(removed[0], added[0]);
  } else if (added.length === 1 && removed.length === 0) {
    emitDeviceInsert(added[0]);
  } else if (removed.length === 1 && added.length === 0) {
    emitDeviceDelete(removed[0]);
  }
};

// Дзеркало _on_devices. suppressStruct -- те саме глушіння, що в bridge:
// застосування чужого DeviceLoad не має повернутись автоемісією назад.
const onDevices = (suppressStruct) => {
  if (!suppressStruct) diffDevices();
  primeDevices();
  primeArrangement();
};

function buildRegistry() {
  song.tracks.forEach((t) => (t.id = newId()));
  song.scenes.forEach((s) => (s.id = newId()));
  refreshAuxTrackIds([], true);
  refreshChainIds();
  primeDevices();
  primeArrangement();
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
  primeDevices();
  primeArrangement();
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
    case 'CueSet': {
      const t = Number(payload.time);
      if (!(t >= 0)) return reject(`некоректна позиція локатора ${payload.time}`);
      song.cues[t] = String(payload.name || '').slice(0, 64);
      break;
    }
    case 'CueDelete': {
      const t = Number(payload.time);
      if (!(t in song.cues)) break;   // tombstone: локатора вже немає
      delete song.cues[t];
      break;
    }
    case 'ClipPropSet': {
      const t = trackById(payload.track?.id);
      const s = sceneIdx(payload.scene?.id);
      if (!t) return reject('невідомий трек');
      if (s < 0) return reject('невідома сцена');
      const check = CLIP_PROPS[payload.prop];
      if (!check) return reject(`невідома властивість кліпу ${payload.prop}`);
      const value = check(payload.value);
      if (value === null) return reject(`некоректне значення ${payload.value} для ${payload.prop}`);
      const clip = t.clips[s];
      if (!clip) break;   // tombstone: кліпа немає, подія мовчки не діє
      clip[payload.prop] = value;
      break;
    }
    case 'SceneTimingSet': {
      const scene = song.scenes.find((x) => x.id === payload.scene?.id);
      if (!scene) return reject('невідома сцена');
      const block = sceneTiming(payload);
      if (!block) return reject('некоректний блок темпу/метру сцени');
      scene.timing = block;
      break;
    }
    case 'SongPropSet': {
      const check = SONG_PROPS[payload.prop];
      if (!check) return reject(`невідома властивість пісні ${payload.prop}`);
      const value = check(payload.value);
      if (value === null) return reject(`некоректне значення ${payload.value} для ${payload.prop}`);
      song.props[payload.prop] = value;
      break;
    }
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
    case 'ObjectMetaSet': {
      const target = metadataTarget(payload);
      if (!target) return reject('невідомий metadata target');
      if (!['name', 'color'].includes(payload.prop)) return reject('невідома metadata property');
      if (payload.prop === 'name' && typeof payload.value !== 'string') return reject('назва не є рядком');
      if (payload.prop === 'color' && (!Number.isInteger(payload.value) || payload.value < 0 || payload.value > 0xffffff)) {
        return reject('колір поза RGB-діапазоном');
      }
      target[payload.prop] = payload.value;
      break;
    }
    case 'TrackCreate': {
      if (trackById(payload.track?.id)) return reject('такий трек уже є');
      // Невідомий різновид не приводиться до відомого: Group Track зі старого
      // скрипта приїжджає як kind:"audio" і породив би фантом
      if (!KNOWN_TRACK_KINDS.has(payload.kind)) {
        return reject(`невідомий різновид треку ${payload.kind}`);
      }
      const idx = Number.isInteger(payload.idx) ? payload.idx : song.tracks.length;
      song.tracks.splice(idx, 0, {
        id: payload.track.id,
        name: payload.track.name,
        color: payload.track.color ?? 0x777777,
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
    case 'TrackDuplicate': {
      if (trackById(payload.track?.id)) return reject('така копія вже є');
      const src = trackById(payload.source?.id);
      const copy = src
        ? JSON.parse(JSON.stringify(src))
        : { name: payload.track?.name || 'copy', color: 0x777777, playing_slot_index: -1,
            slots: song.scenes.length, clips: emptyClips(song.scenes.length),
            devices: [], mix: {}, mute: false, solo: false, arm: false };
      if (!src) console.log(`<- #${gseq} TrackDuplicate: джерела немає, роблю порожній трек`);
      copy.id = payload.track.id;
      copy.playing_slot_index = -1;
      if (payload.track?.name) copy.name = payload.track.name;
      if (payload.track?.color !== undefined) copy.color = payload.track.color;
      const at = src ? song.tracks.indexOf(src) + 1 : song.tracks.length;
      song.tracks.splice(at, 0, copy);
      // Ланцюги копії дістають id детерміновано з локатора -- так само,
      // як їх виведе друга машина.
      const clearChains = (container) => {
        for (const device of container.devices || []) {
          for (const [, chains] of chainGroups(device)) {
            for (const chain of chains) { chain.id = null; clearChains(chain); }
          }
        }
      };
      clearChains(copy);
      refreshChainIds();
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
      song.scenes.splice(idx, 0, {
        id: payload.scene.id,
        name: payload.scene.name || '',
        color: payload.scene.color ?? 0x777777,
      });
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
          length: saneLength(payload.clip?.length),
          name: payload.clip?.name || '',
          color: payload.clip?.color ?? 0x777777,
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
    case 'DeviceInsert': {
      const container = containerByPath(payload.track, payload.chain_path);
      if (!container) return reject('невідомий трек або ланцюг');
      const wanted = String(payload.device?.class_display_name || '');
      const matches = browserNamed().get(wanted.toLowerCase()) || [];
      if (matches.length !== 1) return reject(`немає девайса ${wanted}`);
      const item = matches[0].item;
      const at = Number.isInteger(payload.index) ? payload.index : container.devices.length;
      container.devices.splice(Math.max(0, Math.min(at, container.devices.length)), 0, {
        class_name: item.class_name,
        class_display_name: item.name,
        parameters: [fakeParam('Device On', 1, true)],
      });
      refreshChainIds();
      break;
    }
    case 'DeviceDelete': {
      const container = containerByPath(payload.track, payload.chain_path);
      if (!container) return reject('невідомий трек або ланцюг');
      const device = container.devices[payload.index];
      if (!device) return reject(`девайса за індексом ${payload.index} немає`);
      if (!deviceMatches(device, payload.device)) return reject('за індексом інший девайс');
      container.devices.splice(payload.index, 1);
      refreshChainIds();
      break;
    }
    case 'DeviceMove': {
      const src = containerByPath(payload.from?.track, payload.from?.chain_path);
      const dst = containerByPath(payload.to?.track, payload.to?.chain_path);
      if (!src || !dst) return reject('невідомий трек або ланцюг');
      const device = src.devices[payload.from?.index];
      if (!device) return reject(`девайса за індексом ${payload.from?.index} немає`);
      if (!deviceMatches(device, payload.device)) return reject('за індексом інший девайс');
      src.devices.splice(payload.from.index, 1);
      // Live не клампить сам (виміряно на 12.3.8), ми клампимо: розбіжність
      // станів імовірніша за помилку відправника, і втрачати подію не варто.
      const at = Math.max(0, Math.min(Number(payload.to?.index) || 0, dst.devices.length));
      dst.devices.splice(at, 0, device);
      refreshChainIds();
      break;
    }
    case 'DeviceLoad': {
      const item = browserItem(payload.item);
      if (!item) return reject(`немає девайса ${payload.item?.name || payload.item?.uri}`);
      const t = deviceTrackByRef(payload.track);
      if (!t) return reject('невідомий трек');
      let container = t;
      for (const cref of payload.chain_path || []) {
        container = chainInContainer(container, cref?.id);
        if (!container) return reject('невідомий ланцюг');
      }
      const device = {
        class_name: item.class_name,
        class_display_name: item.name,
        parameters: [fakeParam('Device On', 1, true)],
      };
      const at = Number.isInteger(payload.index) ? payload.index : container.devices.length;
      container.devices.splice(Math.max(0, Math.min(at, container.devices.length)), 0, device);
      refreshChainIds();
      break;
    }
    case 'ClipLoopSet': {
      const t = trackById(payload.track?.id);
      const s = sceneIdx(payload.scene?.id);
      if (!t) return reject('невідомий трек');
      if (s < 0) return reject('невідома сцена');
      const clip = t.clips[s];
      if (!clip) return reject('кліпу немає');
      for (const prop of ['looping', 'loop_start', 'loop_end', 'start_marker', 'end_marker']) {
        if (payload[prop] !== undefined) clip[prop] = payload[prop];
      }
      break;
    }
    case 'ArrangementClipCreate': {
      const t = trackById(payload.track?.id);
      if (!t) return reject('невідомий трек');
      if (!payload.clip?.id) return reject('кліп без uuid');
      if (arrOf(t).some((c) => c.id === payload.clip.id)) break;   // ідемпотентність
      if (payload.clip.is_midi === false) return reject('audio-кліпи в Arrangement не створюємо');
      // Джерело збирається в порожньому слоті Session: LOM інакше не вміє
      if (!t.clips.some((c) => !c)) return reject('усі слоти Session зайняті');
      arrOf(t).push({
        id: payload.clip.id,
        start_time: payload.start_time,
        length: payload.clip.length || NOTE_TIME_SPAN,
        name: payload.clip.name || '',
        color: payload.clip.color ?? 0x777777,
        notes: [],
      });
      arrOf(t).sort((a, b) => a.start_time - b.start_time);
      break;
    }
    case 'ArrangementClipMove': {
      const found = arrClipById(payload.clip?.id);
      if (!found) return reject('немає такого кліпу в Arrangement');
      found.clip.start_time = payload.start_time;
      arrOf(found.track).sort((a, b) => a.start_time - b.start_time);
      break;
    }
    case 'ArrangementClipDelete': {
      const found = arrClipById(payload.clip?.id);
      if (!found) return reject('немає такого кліпу в Arrangement');
      const list = arrOf(found.track);
      list.splice(list.indexOf(found.clip), 1);
      break;
    }
    case 'ArrangementClipNotesSet': {
      const found = arrClipById(payload.clip?.id);
      if (!found) return reject('немає такого кліпу в Arrangement');
      const clip = found.clip;
      clip.notes = (clip.notes || []).filter((note) => !noteInRegion(note, payload.region));
      clip.notes.push(...(payload.notes || []).map((note) => ({ ...note })));
      clip.notes.sort((a, b) => a.start_time - b.start_time || a.pitch - b.pitch);
      break;
    }
    case 'SampleLoad': {
      const rel = payload.sample?.path;
      if (payload.target?.kind === 'arrangement') {
        const at = trackById(payload.track?.id);
        if (!at) return reject('невідомий трек');
        if (!payload.clip?.id) return reject('кліп без uuid');
        if (!sampleExists(rel)) return reject(`семпла ${rel} ще немає в теці проєкту`);
        if (arrOf(at).some((c) => c.id === payload.clip.id)) break;   // ідемпотентність
        if (!at.clips.some((c) => !c)) return reject('усі слоти Session зайняті');
        arrOf(at).push({
          id: payload.clip.id, start_time: payload.target.start_time,
          length: NOTE_TIME_SPAN, name: String(rel).split('/').pop(),
          color: 0x777777, notes: [], kind: 'audio', file_path: rel,
        });
        arrOf(at).sort((a, b) => a.start_time - b.start_time);
        break;
      }
      if (payload.target?.kind === 'drum_pad') {
        const dt = deviceTrackByRef(payload.track);
        if (!dt) return reject('невідомий трек');
        if (!sampleExists(rel)) return reject(`семпла ${rel} ще немає в теці проєкту`);
        const device = findDeviceByRef(dt, payload.target.device);
        if (!device || !isDrumRack(device)) return reject('Drum Rack не знайдено за адресою');
        const note = payload.target.note;
        if (!Number.isInteger(note)) return reject('некоректна нота пада');
        if (padsOf(device)[note]) break;   // ідемпотентність
        padsOf(device)[note] = rel;
        break;
      }
      const t = trackById(payload.track?.id);
      const s = sceneIdx(payload.scene?.id);
      if (!t) return reject('невідомий трек');
      if (payload.target?.kind !== 'slot') return reject(`невідома ціль ${payload.target?.kind}`);
      if (s < 0) return reject('невідома сцена');
      // Подія могла випередити файл -- це не помилка, а гонка з filesync
      if (!sampleExists(rel)) return reject(`семпла ${rel} ще немає в теці проєкту`);
      if (t.clips[s]) break;   // ідемпотентність: у слоті вже щось є
      t.clips[s] = {
        kind: 'audio',
        length: NOTE_TIME_SPAN,
        name: String(rel).split('/').pop(),
        color: 0x777777,
        notes: [],
        file_path: rel,
      };
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
        color: payload.clip?.color ?? 0x777777,
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
  // Дзеркало _on_devices після структурної зміни. Глушіння увімкнене:
  // власне застосування не має полетіти назад автоемісією.
  if (['TrackCreate', 'TrackDelete', 'TrackDuplicate', 'DeviceLoad',
    'DeviceInsert', 'DeviceDelete', 'DeviceMove'].includes(type)) onDevices(true);
  if (type.startsWith('Arrangement')) onArrangement(true);
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
  else if (msg.m === 'view_set') applyViewSet(msg);
  else if (msg.m === 'view_request') sendView();
  else if (msg.m === 'state_request') queueState(msg.id);
  else if (msg.m === 'state_apply') startStateApply(msg.path, msg.id);
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
    case 'cue': {
      const t = Number(rest[0]);
      if (!(t >= 0)) return console.log('некоректна позиція');
      const name = rest.slice(1).join(' ');
      song.cues[t] = name;
      emit('CueSet', { time: t, name });
      console.log(`локатор на ${t}: ${name}`);
      break;
    }
    case 'delcue': {
      const t = Number(rest[0]);
      if (!(t in song.cues)) return console.log('немає локатора на цій позиції');
      delete song.cues[t];
      emit('CueDelete', { time: t });
      console.log(`локатор на ${t} прибрано`);
      break;
    }
    case 'clipprop': {
      const t = song.tracks[Number(rest[0]) || 0];
      const s = Number(rest[1]) || 0;
      const prop = rest[2];
      const check = CLIP_PROPS[prop];
      if (!check) return console.log(`невідома властивість ${prop}`);
      const clip = t && t.clips[s];
      if (!clip) return console.log('немає кліпу в цьому слоті');
      const raw = ['warping', 'muted', 'legato', 'ram_mode'].includes(prop)
        ? rest[3] === 'true' : Number(rest[3]);
      const value = check(raw);
      if (value === null) return console.log(`некоректне значення ${rest[3]}`);
      clip[prop] = value;
      emit('ClipPropSet', {
        track: trackRef(t), scene: sceneRef(song.scenes[s]), prop, value,
      });
      console.log(`кліп ${rest[0]}/${s}: ${prop} = ${value}`);
      break;
    }
    case 'scenetiming': {
      const scene = song.scenes[Number(rest[0]) || 0];
      if (!scene) return console.log('немає такої сцени');
      const block = sceneTiming({
        tempo_enabled: rest[1] !== undefined, tempo: Number(rest[1]),
        time_signature_enabled: rest[2] !== undefined,
        time_signature_numerator: Number(rest[2]),
        time_signature_denominator: Number(rest[3]),
      });
      if (!block) return console.log('некоректні значення');
      scene.timing = block;
      emit('SceneTimingSet', { scene: { id: scene.id }, ...block });
      console.log(`сцена ${rest[0]}: темп ${block.tempo ?? '—'}, метр ${block.time_signature_numerator ?? '—'}/${block.time_signature_denominator ?? '—'}`);
      break;
    }
    case 'songprop': {
      const prop = rest[0];
      const check = SONG_PROPS[prop];
      if (!check) return console.log(`невідома властивість ${prop}`);
      const raw = prop === 'scale_name' ? rest.slice(1).join(' ')
        : ['scale_mode', 'loop', 'punch_in', 'punch_out'].includes(prop)
          ? rest[1] === 'true' : Number(rest[1]);
      const value = check(raw);
      if (value === null) return console.log(`некоректне значення ${rest[1]}`);
      song.props[prop] = value;
      emit('SongPropSet', { prop, value });
      console.log(`${prop} = ${value}`);
      break;
    }
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
      const t = { id: newId(), name: `${idx + 1}-${kind === 'midi' ? 'MIDI' : 'Audio'}`, color: 0x777777, playing_slot_index: -1, slots: song.scenes.length, clips: emptyClips(song.scenes.length), devices: [], mix: {}, mute: false, solo: false, arm: false };
      song.tracks.splice(idx, 0, t);
      emit('TrackCreate', { track: { id: t.id, name: t.name, color: t.color }, idx, kind });
      onDevices(true);
      break;
    }
    case 'deltrack': {
      const t = track();
      if (!t) return console.log('немає такого треку');
      song.tracks.splice(song.tracks.indexOf(t), 1);
      emit('TrackDelete', { track: { id: t.id } });
      onDevices(true);
      break;
    }
    case 'addscene': {
      const idx = rest[0] !== undefined ? Number(rest[0]) : song.scenes.length;
      const s = { id: newId(), name: '', color: 0x777777 };
      song.scenes.splice(idx, 0, s);
      for (const t of song.tracks) {
        t.clips.splice(idx, 0, null);
        t.slots = song.scenes.length;
      }
      emit('SceneCreate', { scene: { id: s.id, color: s.color }, idx });
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
        clip = t.clips[sidx] = { kind: 'midi', length: Math.max(NOTE_TIME_SPAN, start + duration), name: '', color: 0x777777, notes: [] };
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
    case 'meta': {
      const descriptor = metadataFromArg(rest[0]);
      const prop = rest[1];
      if (!descriptor?.target || !['name', 'color'].includes(prop)) {
        return console.log('немає такого metadata target/property');
      }
      const value = prop === 'name' ? rest.slice(2).join(' ') : Number(rest[2]);
      if ((prop === 'name' && typeof value !== 'string') ||
          (prop === 'color' && (!Number.isInteger(value) || value < 0 || value > 0xffffff))) {
        return console.log('некоректне metadata value');
      }
      descriptor.target[prop] = value;
      emit('ObjectMetaSet', { ...metadataAddress(descriptor), prop, value });
      break;
    }
    case 'rename': {
      // перевірка, що uuid переживає те, від чого ламалась адресація за індексом
      const t = track();
      if (!t) return console.log('немає такого треку');
      t.name = rest.slice(1).join(' ');
      emit('ObjectMetaSet', { object: 'track', track: deviceTrackRef(t), prop: 'name', value: t.name });
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
    case 'look': {
      const target = deviceTrackFromArg(rest[0]);
      if (!target) return console.log('немає такого треку');
      song.view.track = target;
      if (rest[1] !== undefined) song.view.scene = song.scenes[Number(rest[1])] || song.view.scene;
      console.log(`дивлюсь на ${song.view.track.name} / ${song.view.scene?.name || '—'}`);
      sendView();
      break;
    }
    case 'view':
      console.log(JSON.stringify(viewPayload()));
      break;
    case 'loop': {
      const t = song.tracks[Number(rest[0]) || 0];
      const s = Number(rest[1]) || 0;
      const clip = t && t.clips[s];
      if (!clip) return console.log('немає кліпу в цьому слоті');
      clip.loop_start = Number(rest[2]) || 0;
      clip.loop_end = Number(rest[3]) || clip.length;
      clip.start_marker = clip.loop_start;
      clip.end_marker = clip.loop_end;
      clip.looping = true;
      emit('ClipLoopSet', {
        track: trackRef(t),
        scene: sceneRef(song.scenes[s]),
        looping: true,
        loop_start: clip.loop_start,
        loop_end: clip.loop_end,
        start_marker: clip.start_marker,
        end_marker: clip.end_marker,
      });
      console.log(`loop ${clip.loop_start}..${clip.loop_end}`);
      break;
    }
    case 'duptrack': {
      const src = song.tracks[Number(rest[0]) || 0];
      if (!src) return console.log('немає такого треку');
      const copy = JSON.parse(JSON.stringify(src));
      copy.id = newId();
      copy.playing_slot_index = -1;
      song.tracks.splice(song.tracks.indexOf(src) + 1, 0, copy);
      const clearChains = (container) => {
        for (const device of container.devices || []) {
          for (const [, chains] of chainGroups(device)) {
            for (const chain of chains) { chain.id = null; clearChains(chain); }
          }
        }
      };
      clearChains(copy);
      refreshChainIds();
      emit('TrackDuplicate', {
        source: { id: src.id },
        track: { id: copy.id, name: copy.name, color: copy.color },
        idx: song.tracks.indexOf(copy),
        kind: /MIDI/i.test(copy.name) ? 'midi' : 'audio',
      });
      onDevices(true);
      console.log(`продубльовано ${src.name} -> ${copy.id}`);
      break;
    }
    case 'arr': {
      // Кладе копію сесійного кліпу в Arrangement -- як duplicate_clip_to_arrangement.
      // Подій не шле: для Arrangement їх поки немає, і в цьому вся суть перевірки.
      const t = song.tracks[Number(rest[0]) || 0];
      const clip = t && t.clips[Number(rest[1]) || 0];
      if (!clip) return console.log('немає кліпу в цьому слоті');
      const start = Number(rest[2]);
      if (!Number.isFinite(start) || start < 0) return console.log('некоректна позиція');
      arrOf(t).push({ id: newId(), start_time: start, length: clip.length, name: clip.name, color: clip.color,
        kind: clip.kind, file_path: clip.file_path,
        notes: (clip.notes || []).map((n) => ({ ...n })) });
      arrOf(t).sort((a, b) => a.start_time - b.start_time);
      onArrangement(false);
      console.log(`в Arrangement ${t.name}: кліп на ${start}-й долі`);
      break;
    }
    case 'movearr': {
      // Прямого сеттера start_time немає, тож переїзд -- це копія плюс видалення.
      // Тут це видно як зміна на місці, але uuid зберігається саме тому,
      // що в bridge джерелом копії служить сам Arrangement-кліп.
      const t = song.tracks[Number(rest[0]) || 0];
      const clip = t && arrOf(t)[Number(rest[1]) || 0];
      const start = Number(rest[2]);
      if (!clip || !Number.isFinite(start)) return console.log('немає такого кліпу або позиції');
      clip.start_time = start;
      arrOf(t).sort((a, b) => a.start_time - b.start_time);
      onArrangement(false);
      console.log(`переїхав на ${start}-ту долю`);
      break;
    }
    case 'delarr': {
      const t = song.tracks[Number(rest[0]) || 0];
      const idx = Number(rest[1]) || 0;
      if (!t || !arrOf(t)[idx]) return console.log('немає такого кліпу');
      arrOf(t).splice(idx, 1);
      onArrangement(false);
      console.log('прибрано з Arrangement');
      break;
    }
    case 'droppad': {
      // Дзеркало живої дії: людина тягне семпл на пад Drum Rack.
      // Ціль -- перший Drum Rack на треку: саме його бачить людина, коли
      // тягне туди семпл, і двозначності тут не буває.
      const t = deviceTrackFromArg(rest[0]);
      const device = t && (t.devices || []).find(isDrumRack);
      const note = Number(rest[1]);
      const rel = rest.slice(2).join(' ');
      if (!device || !isDrumRack(device)) return console.log('на цій позиції не Drum Rack');
      if (!Number.isInteger(note) || note < 0 || note > 127) return console.log('некоректна нота');
      if (!sampleExists(rel)) return console.log(`немає файлу ${rel} у теці проєкту`);
      if (padsOf(device)[note]) return console.log('на паді вже щось лежить');
      padsOf(device)[note] = rel;
      emit('SampleLoad', {
        track: deviceTrackRef(t),
        target: { kind: 'drum_pad', device: deviceRef(t, device), note },
        sample: { path: rel, name: String(rel).split('/').pop() },
      });
      console.log(`поклав ${rel} на пад ${note}`);
      break;
    }
    case 'dropsample': {
      // Дзеркало живої дії: людина тягне семпл із теки проєкту у слот.
      // Live створює audio-кліп сам, а ми лише повідомляємо партнера, ЩО
      // саме і КУДИ покласти -- байти вже їдуть filesync-ом.
      const t = song.tracks[Number(rest[0]) || 0];
      const s = Number(rest[1]) || 0;
      const rel = rest.slice(2).join(' ');
      if (!t || !song.scenes[s]) return console.log('немає такого треку або сцени');
      if (!sampleExists(rel)) return console.log(`немає файлу ${rel} у теці проєкту`);
      if (t.clips[s]) return console.log('слот зайнятий');
      t.clips[s] = {
        kind: 'audio', length: NOTE_TIME_SPAN, name: String(rel).split('/').pop(),
        color: 0x777777, notes: [], file_path: rel,
      };
      emit('SampleLoad', {
        track: trackRef(t),
        scene: sceneRef(song.scenes[s]),
        target: { kind: 'slot' },
        sample: { path: rel, name: String(rel).split('/').pop() },
      });
      console.log(`поклав ${rel} у слот ${s}`);
      break;
    }
    case 'adddevice': {
      // Локальна дія користувача: девайс лягає в сет мовчки, а подію (якщо
      // взагалі) породжує вже діфф -- рівно як у справжньому Live.
      const t = deviceTrackFromArg(rest[0]);
      if (!t) return console.log('немає такого треку');
      const catalog = Object.entries(BROWSER)
        .flatMap(([category, list]) => list.map((item) => ({ category, item })));
      const pick = (name) => catalog.filter((m) =>
        m.item.name.toLowerCase() === String(name).toLowerCase());
      // Назва девайса може містити пробіл ("Drum Rack"), тож спершу
      // пробуємо весь хвіст як назву, і лише потім -- перше слово плюс пресет.
      const whole = rest.slice(1).join(' ');
      let found = pick(whole);
      let presetFrom = rest.length;
      if (!found.length) {
        found = pick(rest[1] || '');
        presetFrom = 2;
      }
      if (!found.length) return console.log(`немає девайса ${whole}`);
      const device = {
        class_name: found[0].item.class_name,
        class_display_name: found[0].item.name,
        parameters: [fakeParam('Device On', 1, true)],
      };
      // Пресет -- лише те, що лишилось ПІСЛЯ назви девайса
      if (rest[presetFrom]) device.name = rest.slice(presetFrom).join(' ');
      t.devices.push(device);
      refreshChainIds();
      onDevices(false);
      console.log(`поклав ${deviceName(device)} на ${t.name}`);
      break;
    }
    case 'movemode': {
      const wanted = String(rest[0] || '').toLowerCase();
      if (wanted !== 'move' && wanted !== 'pair') return console.log('режим: move або pair');
      deviceMoveMode = wanted;
      console.log(`режим переїзду: ${deviceMoveMode}`);
      break;
    }
    case 'deldevice': {
      const t = deviceTrackFromArg(rest[0]);
      if (!t) return console.log('немає такого треку');
      const at = Number(rest[1]);
      if (!t.devices[at]) return console.log('немає девайса за цим індексом');
      const [gone] = t.devices.splice(at, 1);
      refreshChainIds();
      onDevices(false);
      console.log(`зняв ${deviceName(gone)} з ${t.name}`);
      break;
    }
    case 'movedevice': {
      const from = deviceTrackFromArg(rest[0]);
      const to = deviceTrackFromArg(rest[2]);
      if (!from || !to) return console.log('немає такого треку');
      const at = Number(rest[1]);
      const device = from.devices[at];
      if (!device) return console.log('немає девайса за цим індексом');
      from.devices.splice(at, 1);
      const target = Math.max(0, Math.min(Number(rest[3]) || 0, to.devices.length));
      to.devices.splice(target, 0, device);
      refreshChainIds();
      onDevices(false);
      console.log(`переніс ${deviceName(device)} на ${to.name}[${target}]`);
      break;
    }
    case 'load': {
      const t = deviceTrackFromArg(rest[0]);
      if (!t) return console.log('немає такого треку');
      const category = rest[2] || 'audio_effects';
      const item = browserItem({ uri: rest[1], category });
      if (!item) return console.log(`немає девайса ${rest[1]} у ${category}`);
      emit('DeviceLoad', {
        track: deviceTrackRef(t),
        item: { uri: item.uri, name: item.name, category, class_name: item.class_name },
      });
      console.log(`просив завантажити ${item.name}`);
      break;
    }
    case 'fullstate':
      queueState();
      break;
    case 'state':
      console.log(JSON.stringify(snapshot(), null, 2));
      break;
    case '':
      break;
    default:
      console.log([
        'play | stop | tempo <bpm>',
        'fullstate -- повний знімок сету чанками',
        'look <track|return:N|master> [scene] | view',
        'loop <track> <scene> <start> <end> | duptrack <track>',
        'load <track> <uri> [category]',
        'launch <t> <s> | scene <n> | stopclip <t> | stopall',
        'note <t> <s> <pitch> <start> <duration> [velocity] | delnote <t> <s> <pitch> <start> | delclip <t> <s>',
        'device <track> <device[/chain/device...]> <parameter> <value> | vol <t> <value> | pan <t> <value> | send <t> <index> <value>',
        'addtrack [midi|audio] [idx] | deltrack <t> | addscene [idx] | delscene <n>',
        'meta <track:N|return:N|master|scene:N|clip:T:S> <name|color> <value> | rename <t> <name> | move <from> <to> | state',
      ].join('\n'));
  }
});
