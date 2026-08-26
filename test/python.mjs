// Запускає Python-перевірки планувальника з-під того самого `npm test`.
//
// chat.py живе всередині Live, тож JS-дзеркала в нього немає й бути не може.
// Окрема команда, яку треба памʼятати, -- це команда, яку не запускають, тож
// набір має бути один.
//
// Інтерпретатора може не бути на PATH (демон працює й без нього). Тоді
// перевірка не падає, але й не мовчить: пропуск друкується так само помітно,
// як падіння, а MP_REQUIRE_PY=1 робить його падінням для CI.

import test from 'node:test';
import assert from 'node:assert';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');

function findPython() {
  for (const name of ['python3', 'python', 'py']) {
    const probe = spawnSync(name, ['-c', 'import sys; print(sys.version_info[0])'],
      { encoding: 'utf8' });
    if (probe.status === 0 && probe.stdout.trim() === '3') return name;
  }
  return null;
}

test('планувальник: блоки, строк на спроби, зупинка черги', () => {
  const python = findPython();
  if (!python) {
    const text = 'python3 не знайдено на PATH — перевірки chat.py ПРОПУЩЕНО';
    if (process.env.MP_REQUIRE_PY) assert.fail(text);
    console.log(`\n  !! ${text}\n     постав Python 3 або запусти test/planner_test.py вручну\n`);
    return;
  }
  const run = spawnSync(python, ['-m', 'unittest', 'discover', '-s', 'test',
    '-p', 'planner_test.py'], { cwd: root, encoding: 'utf8' });
  if (run.status !== 0) {
    assert.fail(`${run.stdout || ''}\n${run.stderr || ''}`.trim());
  }
  // unittest пише підсумок у stderr; переконуємось, що тести реально бігли
  assert.match(`${run.stderr}`, /Ran \d+ tests/, 'unittest нічого не запустив');
});
