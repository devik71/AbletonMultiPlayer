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
  assert.equal(compareStates(mine, theirs, { limit: 5 }).length, 5);
});
