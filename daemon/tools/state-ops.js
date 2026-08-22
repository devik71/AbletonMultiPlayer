// Знімок стану -> послідовність звичайних подій.
//
// Дзеркало _state_to_ops із Remote Script. Окремого шляху запису в LOM немає
// ні тут, ні в bridge: знімок застосовується тими самими подіями, що й журнал,
// інакше довелось би тримати дві реалізації однієї семантики.

const NOTE_TIME_SPAN = 4;
// Дзеркало CLIP_LENGTH_MAX із bridge: заглушка Live під час запису
// (два роки в секундах) не сміє стати регіоном на два роки.
const CLIP_LENGTH_MAX = 1e6;
const NOTES_PER_REGION = 1024;

export const metaOps = (kind, ref, src) => ['name', 'color']
  .filter((prop) => src[prop] !== undefined && src[prop] !== null)
  .map((prop) => ['ObjectMetaSet', {
    object: kind, prop, value: src[prop], [kind === 'scene' ? 'scene' : 'track']: ref,
  }]);

const mixerOps = (ref, mixer) => {
  const ops = [];
  for (const param of ['volume', 'panning', 'crossfader', 'cue_volume']) {
    if (param in mixer) ops.push(['MixerSet', { track: ref, param, value: mixer[param] }]);
  }
  for (const send of mixer.sends || []) {
    if (send?.value === undefined) continue;
    ops.push(['MixerSet', { track: ref, param: 'send', index: send.index, value: send.value }]);
  }
  for (const prop of ['mute', 'solo', 'arm']) {
    if (prop in mixer) ops.push(['TrackToggle', { track: ref, param: prop, value: !!mixer[prop] }]);
  }
  return ops;
};

const deviceOps = (ref, devices) => devices.flatMap((entry) =>
  (entry.parameters || []).filter((p) => p.value !== undefined && p.value !== null).map((p) => {
    const payload = {
      track: ref,
      device: entry.device,
      parameter: { name: p.name, ordinal: p.ordinal },
      value: p.value,
    };
    if (entry.chain_path?.length) payload.chain_path = entry.chain_path;
    return ['DeviceParamSet', payload];
  }));

export const noteRegionsFor = (meta, notes) => {
  let length = Math.max(Number(meta.length) || NOTE_TIME_SPAN, 0.001);
  if (length > CLIP_LENGTH_MAX) length = NOTE_TIME_SPAN;
  const ordered = [...notes].sort((a, b) =>
    (a.start_time - b.start_time) || (a.pitch - b.pitch));
  const end = ordered.reduce((acc, note) => Math.max(acc, (note.start_time || 0) + 0.001), length);
  const whole = { from_pitch: 0, pitch_span: 128, from_time: 0, time_span: end };
  if (!ordered.length) return [[whole, []]];

  const groups = [[]];
  for (const note of ordered) {
    const current = groups[groups.length - 1];
    if (current.length >= NOTES_PER_REGION && note.start_time !== current[current.length - 1].start_time) {
      groups.push([note]);
    } else {
      current.push(note);
    }
  }
  if (groups.length === 1) return [[whole, groups[0]]];

  const regions = [];
  let start = 0;
  groups.forEach((group, i) => {
    let stop = i === groups.length - 1 ? end : groups[i + 1][0].start_time;
    if (stop <= start) stop = start + 0.001;
    regions.push([{ from_pitch: 0, pitch_span: 128, from_time: start, time_span: stop - start }, group]);
    start = stop;
  });
  return regions;
};

const clipOps = (ref, clips) => clips.flatMap((entry) => {
  const scene = entry.scene || {};
  if (!scene.id) return [];
  const meta = entry.clip || {};
  const ops = [];
  if (entry.notes) {
    ops.push(['ClipCreate', { track: ref, scene, clip: meta }]);
    for (const [region, part] of noteRegionsFor(meta, entry.notes)) {
      ops.push(['ClipNotesSet', { track: ref, scene, clip: meta, region, notes: part }]);
    }
  }
  for (const prop of ['name', 'color']) {
    if (meta[prop] !== undefined && meta[prop] !== null) {
      ops.push(['ObjectMetaSet', { object: 'clip', track: ref, scene, prop, value: meta[prop] }]);
    }
  }
  return ops;
});

export function stateToOps(state) {
  const ops = [];
  if (typeof state.tempo === 'number') ops.push(['TempoSet', { bpm: state.tempo }]);
  for (const track of state.tracks || []) {
    if (!track.id) continue;
    const ref = { id: track.id };
    ops.push(...metaOps('track', ref, track), ...mixerOps(ref, track.mixer || {}),
      ...deviceOps(ref, track.devices || []), ...clipOps(ref, track.clips || []));
  }
  for (const aux of state.aux_tracks || []) {
    if (!aux.id || !aux.kind) continue;
    const ref = { id: aux.id, kind: aux.kind };
    ops.push(...metaOps('track', ref, aux), ...mixerOps(ref, aux.mixer || {}),
      ...deviceOps(ref, aux.devices || []));
  }
  for (const scene of state.scenes || []) {
    if (scene.id) ops.push(...metaOps('scene', { id: scene.id }, scene));
  }
  return ops;
}
