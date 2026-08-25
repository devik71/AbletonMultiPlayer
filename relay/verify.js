// Перевірка цілісності сесії: холодний архів, живий журнал і checkpoint.
//
//   node verify.js <session> [--dir <тека журналів>]
//
// Стиснутий журнал має законні дірки в gseq, тож повний ланцюг перевіряється
// за архівом -- там історія ціла. Цей скрипт і є те "перевіряється за архівом",
// на яке посилається docs/PROTOCOL.md.

import { createHash } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));

function canonical(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  return '{' + Object.keys(value).sort()
    .map((k) => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
}

const hashEvent = (prevHash, body) =>
  createHash('sha256').update(prevHash + canonical(body)).digest('hex');

function readJournal(path) {
  return readFileSync(path, 'utf8').split('\n').filter(Boolean).map((line, i) => {
    try {
      return JSON.parse(line);
    } catch {
      throw new Error(`${path}: рядок ${i + 1} не парситься`);
    }
  });
}

const problems = [];
const notes = [];
const fail = (text) => problems.push(text);

function checkSelfHash(events, where) {
  for (const ev of events) {
    const { hash, prev_hash: prev, ...body } = ev;
    if (hashEvent(prev, body) !== hash) fail(`${where}: подія #${ev.gseq} не збігається зі своїм хешем`);
  }
}

/** Архів append-only: після падіння в ньому може повторитись діапазон. */
function dedupeArchive(events) {
  const byGseq = new Map();
  let repeated = 0;
  for (const ev of events) {
    const seen = byGseq.get(ev.gseq);
    if (!seen) {
      byGseq.set(ev.gseq, ev);
      continue;
    }
    repeated += 1;
    if (seen.hash !== ev.hash) fail(`архів: дві різні події з gseq=${ev.gseq}`);
  }
  if (repeated) notes.push(`архів містить ${repeated} повторених записів (падіння під час стиснення) -- це нормально`);
  return [...byGseq.values()].sort((a, b) => a.gseq - b.gseq);
}

function checkChain(events, where) {
  let prevHash = '';
  let expected = 1;
  for (const ev of events) {
    if (ev.gseq !== expected) {
      fail(`${where}: пропущено gseq=${expected} (далі йде #${ev.gseq})`);
      return;
    }
    if (ev.prev_hash !== prevHash) {
      fail(`${where}: ланцюг розірвано на #${ev.gseq}`);
      return;
    }
    prevHash = ev.hash;
    expected += 1;
  }
}

const args = process.argv.slice(2);
const session = args.find((a) => !a.startsWith('--'));
const dirFlag = args.indexOf('--dir');
const journalDir = dirFlag >= 0 && args[dirFlag + 1]
  ? args[dirFlag + 1]
  : (process.env.MP_JOURNAL_DIR || join(__dirname, 'journals'));

if (!session) {
  console.error('вкажи сесію: node verify.js <session> [--dir <тека>]');
  process.exit(2);
}

const livePath = join(journalDir, `${session}.jsonl`);
const archivePath = join(journalDir, `${session}.archive.jsonl`);
const checkpointPath = join(journalDir, `${session}.checkpoint.json`);

if (!existsSync(livePath)) {
  console.error(`немає журналу ${livePath}`);
  process.exit(2);
}

const live = readJournal(livePath);
const archive = existsSync(archivePath) ? dedupeArchive(readJournal(archivePath)) : [];
const checkpoint = existsSync(checkpointPath)
  ? JSON.parse(readFileSync(checkpointPath, 'utf8')) : null;

checkSelfHash(live, 'журнал');
checkSelfHash(archive, 'архів');
if (archive.length) checkChain(archive, 'архів');

const compacted = checkpoint?.compacted === true;
if (!compacted) checkChain(live, 'журнал');

// Живий журнал -- підпослідовність історії: усе, що вже в архіві, має збігатись
// з ним подія в подію.
const archived = new Map(archive.map((ev) => [ev.gseq, ev]));
for (const ev of live) {
  const cold = archived.get(ev.gseq);
  if (cold && cold.hash !== ev.hash) fail(`журнал і архів розходяться на #${ev.gseq}`);
  if (!cold && checkpoint && ev.gseq <= (checkpoint.archived_through ?? 0)) {
    fail(`архів не містить #${ev.gseq}, хоч і оголошений повним до #${checkpoint.archived_through}`);
  }
}

const head = live[live.length - 1];
if (checkpoint) {
  if (Number.isSafeInteger(checkpoint.events) && live.length < checkpoint.events) {
    fail(`у журналі ${live.length} подій замість ${checkpoint.events} із checkpoint`);
  }
  if (head && checkpoint.gseq === head.gseq && checkpoint.hash !== head.hash) {
    fail('checkpoint не збігається з головою журналу');
  }
  if (head && checkpoint.gseq > head.gseq) fail('checkpoint попереду журналу');
} else {
  notes.push('checkpoint відсутній -- перевірено лише журнал та архів');
}

console.log(`сесія ${session} у ${journalDir}`);
console.log(`  журнал: ${live.length} подій, head #${head?.gseq ?? 0}${compacted ? ', стиснутий' : ''}`);
console.log(`  архів: ${archive.length} подій${archive.length ? ` до #${archive[archive.length - 1].gseq}` : ''}`);
for (const note of notes) console.log(`  · ${note}`);

// Розкладка журналу по типах і авторах. Після сесії це перше, на що
// дивишся: чого було багато, хто це слав і чи не роздувся якийсь тип.
const all = archive.length ? archive : live;
if (all.length) {
  const byType = new Map();
  const byAuthor = new Map();
  let biggest = null;
  for (const ev of all) {
    const size = JSON.stringify(ev.payload ?? {}).length;
    const t = byType.get(ev.type) || { n: 0, bytes: 0 };
    t.n += 1; t.bytes += size;
    byType.set(ev.type, t);
    byAuthor.set(ev.author, (byAuthor.get(ev.author) || 0) + 1);
    if (!biggest || size > biggest.size) biggest = { size, type: ev.type, gseq: ev.gseq };
  }
  const top = [...byType].sort((a, b) => b[1].n - a[1].n).slice(0, 8);
  console.log('  типи: ' + top.map(([type, x]) => `${type}×${x.n}`).join(', ') +
              (byType.size > top.length ? `, ще ${byType.size - top.length} типів` : ''));
  console.log('  автори: ' + [...byAuthor].map(([a, n]) => `${a}×${n}`).join(', '));
  if (biggest) {
    console.log(`  найбільша подія: ${biggest.type} #${biggest.gseq}, ${biggest.size} символів`);
  }
}

if (problems.length) {
  console.log('');
  for (const problem of problems) console.log(`  ПРОБЛЕМА: ${problem}`);
  process.exit(1);
}
console.log('  цілісність підтверджена');
