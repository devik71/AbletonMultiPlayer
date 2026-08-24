#!/usr/bin/env node
/**
 * Чи той Remote Script стоїть у Live, що лежить у репозиторії.
 *
 * Приводом була реальна поразка: Live десять днів виконував копію від
 * 14 серпня, поки в репозиторії росли Arrangement, групи й семпли. Живі
 * прогони весь той час перевіряли старий код, і жоден із них нічого не
 * підтверджував. Дешевше питати щоразу, ніж помітити через тиждень.
 *
 *   node tools/check-install.mjs [--install]
 *
 * Без прапорця лише звіряє (код виходу 1 при розбіжності). З --install
 * перезаписує встановлену копію файлами з репозиторію.
 */
import { copyFileSync, existsSync, readFileSync, rmSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const SOURCE = join(ROOT, "remote-script", "AbletonMP");
const FILES = ["AbletonMP.py", "chat.py", "link.py", "registry.py", "__init__.py"];

/** Live тримає користувацькі скрипти в User Library, а її шлях залежить від
 *  мови Windows і від того, чи перехопив теку OneDrive. Тому не вгадуємо. */
function candidates() {
  const home = homedir();
  const out = [];
  const docs = ["Documents", "Документы", "Документи", "Dokumente", "Documenti"];
  const bases = [home, join(home, "OneDrive")];
  for (const base of bases) {
    for (const doc of docs) {
      out.push(join(base, doc, "Ableton", "User Library", "Remote Scripts", "AbletonMP"));
    }
  }
  out.push(join(home, "Music", "Ableton", "User Library", "Remote Scripts", "AbletonMP"));
  return out;
}

function findInstall() {
  for (const path of candidates()) {
    if (existsSync(join(path, "AbletonMP.py"))) return path;
  }
  // Остання спроба: раптом тека є, але порожня — це теж треба показати.
  for (const path of candidates()) {
    if (existsSync(path)) return path;
  }
  return null;
}

const install = findInstall();
if (install === null) {
  console.log("не знайшов встановленої копії AbletonMP у User Library");
  console.log("шукав у:");
  for (const path of candidates()) console.log("  " + path);
  process.exit(1);
}

const wantInstall = process.argv.includes("--install");
const diffs = [];

for (const file of FILES) {
  const from = join(SOURCE, file);
  const to = join(install, file);
  if (!existsSync(to)) { diffs.push(`${file}: не встановлений`); continue; }
  if (readFileSync(from).equals(readFileSync(to))) continue;
  diffs.push(`${file}: відрізняється`);
}

if (wantInstall) {
  for (const file of FILES) copyFileSync(join(SOURCE, file), join(install, file));
  // Стара компіляція поруч зі свіжим джерелом — зайвий шанс, що Live візьме її.
  const cache = join(install, "__pycache__");
  if (existsSync(cache)) rmSync(cache, { recursive: true, force: true });
  console.log(`встановлено в ${install}`);
  console.log("Live треба перезапустити, щоб він перечитав скрипт");
  process.exit(0);
}

console.log(`встановлена копія: ${install}`);
if (diffs.length === 0) {
  console.log("збіг з репозиторієм");
} else {
  for (const line of diffs) console.log("  ✗ " + line);
  console.log("\nживі прогони зараз перевіряють НЕ той код, що в репозиторії");
  console.log("полагодити: node tools/check-install.mjs --install  (далі перезапустити Live)");
  process.exit(1);
}
