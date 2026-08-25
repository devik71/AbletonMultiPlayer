// Одна команда перед прогоном парою: усе, що можна перевірити без партнера.
//
// Порядок від дешевого до дорогого, і зупиняємось на першій же поломці --
// далі йти немає сенсу, а сесія коштує двох людей.

import { spawnSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const steps = [
  ['память проєкту', 'node', ['tools/memory.mjs', 'verify']],
  ['скрипт у Live', 'node', ['tools/check-install.mjs']],
  ['тести', 'npm', ['test']],
];

let failed = null;
for (const [label, cmd, args] of steps) {
  process.stdout.write(`\n══ ${label} ══\n`);
  // Рядком, а не масивом: масив із shell:true лише склеює аргументи без
  // екранування, а npm на Windows -- це .cmd, який без shell не запуститься.
  const run = spawnSync([cmd, ...args].join(' '), { cwd: root, stdio: 'inherit', shell: true });
  if (run.status !== 0) { failed = label; break; }
}

if (failed) {
  console.log(`\nзупинився на кроці «${failed}» — далі йти немає сенсу`);
  process.exit(1);
}

console.log([
  '',
  'усе чисте. Лишилось те, що без партнера не перевіряється:',
  '',
  '  node tools/pair-probe.mjs      весь протокольний бік з емулятором',
  '                                 замість другої машини (потрібні',
  '                                 запущені relay і daemon)',
  '',
  'а далі -- docs/FIRST-RUN.md, розділ «Сценарій прогону».',
].join('\n'));
