// Порівняння знімків: діагностика мусить називати розбіжність, а не мовчати.

import assert from 'node:assert/strict';
import test from 'node:test';
import { compareStates } from '../daemon/compare.js';

const base = () => ({
  tempo: 120,
  song: { signature_numerator: 4, root_note: 0 },
  cues: [{ time: 32, name: 'Drop' }],
  scenes: [{ id: 's1', name: 'A' }, { id: 's2', name: 'B' }],
  tracks: [{
    id: 't1', name: 'Bass',
    devices: [{ device: { class_display_name: 'Auto Filter' } }],
    clips: [{ scene: { id: 's1' }, clip: { length: 4 }, notes: [{ pitch: 60 }] }],
    arrangement: [],
  }],
});

test('однакові знімки не дають жодного рядка', () => {
  assert.deepEqual(compareStates(base(), base()), []);
});

test('розбіжність називається предметно, з обох боків', () => {
  const mine = base();
  const theirs = base();
  theirs.tempo = 128;
  theirs.song.signature_numerator = 6;
  theirs.cues = [];
  theirs.tracks[0].name = 'Lead';
  theirs.tracks[0].devices = [];
  theirs.tracks[0].clips[0].notes = [{ pitch: 60 }, { pitch: 64 }];
  theirs.scenes.push({ id: 's3', name: 'C' });

  const lines = compareStates(mine, theirs).join('\n');
  assert.match(lines, /темп: у тебе 120, у партнера 128/);
  assert.match(lines, /signature_numerator: у тебе 4, у партнера 6/);
  assert.match(lines, /локатор «Drop».*є в тебе, у партнера немає/);
  assert.match(lines, /сцена «C» є в партнера, у тебе немає/);
  assert.match(lines, /у тебе «Bass», у партнера «Lead»/);
  assert.match(lines, /девайси у тебе \[Auto Filter\], у партнера \[—\]/);
  assert.match(lines, /нот 1 проти 2/);
});

test('відсутній кліп називається з того боку, де його бракує', () => {
  const mine = base();
  const theirs = base();
  theirs.tracks[0].clips = [];
  assert.match(compareStates(mine, theirs).join('\n'),
    /кліп у сцені s1 є в тебе, у партнера немає/);
  assert.match(compareStates(theirs, mine).join('\n'),
    /кліп у сцені s1 є в партнера, у тебе немає/);
});

test('перелік розбіжностей обмежений, а не нескінченний', () => {
  const mine = base();
  const theirs = base();
  for (let i = 0; i < 100; i += 1) theirs.scenes.push({ id: `x${i}`, name: `S${i}` });
  const lines = compareStates(mine, theirs, { limit: 5 });
  assert.equal(lines.length, 6, "пʼять рядків плюс підсумок");
  assert.match(lines[5], /…і ще \d+ розбіжностей/);
});

test('однакова структура з різними значеннями теж називається', () => {
  const mk = () => ({
    tracks: [{
      id: 't1', name: 'Bass',
      mixer: { sends: [{ index: 0, value: 0.2 }] },
      devices: [{
        device: { class_name: 'AutoFilter2', class_display_name: 'Auto Filter', ordinal: 0 },
        parameters: [{ name: 'Frequency', ordinal: 0, value: 0.4 },
                     { name: 'Res', ordinal: 0, value: 0.1 }],
      }],
      clips: [{
        scene: { id: 's1' }, clip: { length: 4 }, notes: [],
        props: { gain: 0.5 },
        warp: [{ beat_time: 0, sample_time: 0 }],
      }],
      arrangement: [],
    }],
  });
  const mine = mk();
  const theirs = mk();
  theirs.tracks[0].devices[0].parameters[0].value = 0.9;
  theirs.tracks[0].mixer.sends[0].value = 0.7;
  theirs.tracks[0].clips[0].props.gain = 0.1;
  theirs.tracks[0].clips[0].warp.push({ beat_time: 4, sample_time: 2 });

  const lines = compareStates(mine, theirs).join('\n');
  assert.match(lines, /Auto Filter: розходяться 1 параметрів/);
  assert.match(lines, /Frequency 0\.4≠0\.9/);
  assert.match(lines, /сенд 0 0\.2 проти 0\.7/);
  assert.match(lines, /gain 0\.5 проти 0\.1/);
  assert.match(lines, /warp-маркери різні \(1 проти 2\)/);

  // структура однакова -- отже жодного рядка про відсутні обʼєкти
  assert.ok(!/немає/.test(lines), `зайвий рядок про відсутність: ${lines}`);
});

test('розбіжність у Return-треках названа окремо: вона ламає сенди', () => {
  const mine = { aux_tracks: [{ id: 'r1', kind: 'return', name: 'A' },
                              { id: 'r2', kind: 'return', name: 'B' }] };
  const theirs = { aux_tracks: [{ id: 'r1', kind: 'return', name: 'A' }] };
  const lines = compareStates(mine, theirs).join('\n');
  assert.match(lines, /return «B» є в тебе, у партнера немає/);
  assert.match(lines, /Return-треків 2 проти 1 — індекси сендів означають різне/);
});

test('розбіжність у кліпах лінійки називається поіменно, а не кількістю', () => {
  const mk = (over) => ({
    tracks: [{
      id: 't1', name: 'Bass', clips: [],
      arrangement: [{ id: 'a1', start_time: 16, props: { gain: 0.5 },
                      warp: [{ beat_time: 0, sample_time: 0 }], notes: [], ...over }],
    }],
  });
  const lines = compareStates(mk(), mk({ start_time: 32, props: { gain: 0.9 } })).join('\n');
  assert.match(lines, /кліп у лінійці на 16-й долі проти 32-ї/);
  assert.match(lines, /лінійка: gain 0\.5 проти 0\.9/);

  // Кількість однакова, а кліпи різні -- старе порівняння цього не бачило
  const other = compareStates(mk(), {
    tracks: [{ id: 't1', name: 'Bass', clips: [],
               arrangement: [{ id: 'a2', start_time: 16 }] }],
  }).join('\n');
  assert.match(other, /є в тебе, у партнера немає/);
  assert.match(other, /є в партнера, у тебе немає/);
});

test('розʼїхані фейдери названі: досі diff порівнював лише сенди', () => {
  const state = (volume, mute) => ({
    tempo: 120,
    tracks: [{ id: 't1', name: 'Bass', mixer: { volume, mute, sends: [] }, clips: [] }],
    aux_tracks: [{ id: 'r1', kind: 'return', name: 'Reverb', mixer: { volume: 0.5 } }],
    scenes: [],
  });
  const out = compareStates(state(0.85, false), state(0.4, true));
  assert.ok(out.some((l) => l.includes('volume')), `гучність не названа: ${out.join(' | ')}`);
  assert.ok(out.some((l) => l.includes('mute')), `mute не названий: ${out.join(' | ')}`);
});

test('гучність Return порівнюється: від неї залежить увесь сет', () => {
  const state = (volume) => ({
    tracks: [],
    aux_tracks: [{ id: 'r1', kind: 'return', name: 'Reverb', mixer: { volume } }],
    scenes: [],
  });
  const out = compareStates(state(0.7), state(0.2));
  assert.ok(out.some((l) => l.includes('Reverb') && l.includes('volume')),
    `Return не порівняний: ${out.join(' | ')}`);
});

test('різні межі кліпу -- це розбіжність, а не збіг', () => {
  const state = (loopEnd) => ({
    tracks: [{
      id: 't1', name: 'Drums', mixer: {},
      clips: [{ scene: { id: 's1' }, clip: { length: 4 },
        loop: { looping: true, loop_start: 0, loop_end: loopEnd } }],
      arrangement: [{ id: 'a1', start_time: 0, length: 8,
        loop: { looping: true, loop_start: 0, loop_end: loopEnd } }],
    }],
    scenes: [{ id: 's1', name: 'A' }],
  });
  const out = compareStates(state(4), state(2));
  assert.equal(out.filter((l) => l.includes('межі кліпу')).length, 2,
    `межі не названі з обох боків: ${out.join(' | ')}`);
});

test('темп сцени й колір треку теж розбіжність', () => {
  const state = (tempo, color) => ({
    tracks: [{ id: 't1', name: 'Bass', color, mixer: {}, clips: [] }],
    scenes: [{ id: 's1', name: 'A', timing: { tempo, tempo_enabled: true } }],
  });
  const out = compareStates(state(120, 16711680), state(140, 255));
  assert.ok(out.some((l) => l.includes('темп/метр')), `темп сцени не названий: ${out.join(' | ')}`);
  assert.ok(out.some((l) => l.includes('колір')), `колір не названий: ${out.join(' | ')}`);
});
