#!/usr/bin/env node
/**
 * Пам'ять проєкту: факти, які переживають переїзд між машинами.
 *
 * Навіщо окремий інструмент, а не ще один markdown-файл: пам'ять псується
 * тихо. Запис застаріває, ніхто цього не бачить, і наступна сесія будує
 * рішення на неправді. Тому кожен запис може нести команду перевірки, а
 * `check` її виконує -- пам'ять звіряється з дійсністю, а не з довірою.
 *
 * Один запис -- один файл. Це не естетика: дві машини, що правлять спільний
 * MEMORY.md, дають конфлікт злиття на кожному коміті, а окремі файли
 * зливаються самі.
 *
 * Використання:
 *   node tools/memory.mjs list [--type T] [--tag T] [--scope S] [--all]
 *   node tools/memory.mjs show <id>
 *   node tools/memory.mjs add <id> --type T --title "..." [--body "..."]
 *                                  [--why "..."] [--tags a,b] [--scope S]
 *                                  [--verify "cmd"] [--expect "текст"]
 *   node tools/memory.mjs supersede <id> --by <new-id> [--reason "..."]
 *   node tools/memory.mjs verify [<id>...]
 *   node tools/memory.mjs check
 *   node tools/memory.mjs index
 */
import { execSync } from "node:child_process";
import { readdirSync, readFileSync, writeFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const DIR = join(ROOT, "docs", "memory");
const INDEX = join(DIR, "INDEX.md");

const TYPES = ["state", "decision", "env", "reference"];
const TYPE_LABEL = {
  state: "стан",
  decision: "рішення",
  env: "оточення",
  reference: "посилання",
};

// Скільки днів запис типу state вважається свіжим. Рішення й посилання не
// протухають від часу -- лише від того, що їх скасували.
const STALE_DAYS = 30;

// ── читання ────────────────────────────────────────────────────────────────

function today() {
  return new Date().toISOString().slice(0, 10);
}

function parse(file) {
  const raw = readFileSync(join(DIR, file), "utf8");
  const m = raw.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/);
  if (!m) return { file, broken: "немає frontmatter" };
  const meta = {};
  for (const line of m[1].split(/\r?\n/)) {
    if (!line.trim()) continue;
    const at = line.indexOf(":");
    if (at < 0) return { file, broken: `рядок без двокрапки: ${line}` };
    const key = line.slice(0, at).trim();
    let value = line.slice(at + 1).trim();
    if (value.startsWith("[") && value.endsWith("]")) {
      value = value.slice(1, -1).split(",").map((s) => s.trim()).filter(Boolean);
    } else if (value.startsWith('"') && value.endsWith('"')) {
      value = value.slice(1, -1);
    }
    meta[key] = value;
  }
  return { file, meta, body: m[2].trim() };
}

function load() {
  if (!existsSync(DIR)) return [];
  return readdirSync(DIR)
    .filter((f) => f.endsWith(".md") && f !== "INDEX.md")
    .map(parse)
    .sort((a, b) => (a.meta?.id ?? a.file).localeCompare(b.meta?.id ?? b.file));
}

// ── запис ──────────────────────────────────────────────────────────────────

function quote(value) {
  return /[:#]/.test(value) ? `"${value.replace(/"/g, "'")}"` : value;
}

function render(meta, body) {
  const order = ["id", "type", "title", "scope", "status", "updated",
                 "tags", "verify", "expect", "supersedes", "superseded_by"];
  const lines = ["---"];
  for (const key of order) {
    const value = meta[key];
    if (value === undefined || value === "" ||
        (Array.isArray(value) && value.length === 0)) continue;
    lines.push(`${key}: ${Array.isArray(value) ? `[${value.join(", ")}]` : quote(String(value))}`);
  }
  lines.push("---", "", body.trim(), "");
  return lines.join("\n");
}

function save(id, meta, body) {
  if (!existsSync(DIR)) mkdirSync(DIR, { recursive: true });
  writeFileSync(join(DIR, `${id}.md`), render(meta, body), "utf8");
}

// ── аргументи ──────────────────────────────────────────────────────────────

function parseArgs(argv) {
  const flags = {};
  const positional = [];
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg.startsWith("--")) {
      const key = arg.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith("--")) flags[key] = true;
      else { flags[key] = next; i++; }
    } else positional.push(arg);
  }
  return { flags, positional };
}

function fail(message) {
  process.stderr.write(`memory: ${message}\n`);
  process.exit(1);
}

// ── команди ────────────────────────────────────────────────────────────────

function cmdList({ flags }) {
  const entries = load();
  const rows = entries.filter((e) => {
    if (e.broken) return true;
    if (!flags.all && e.meta.status === "superseded") return false;
    if (flags.type && e.meta.type !== flags.type) return false;
    if (flags.scope && e.meta.scope !== flags.scope && e.meta.scope !== "all") return false;
    if (flags.tag && !(e.meta.tags ?? []).includes(flags.tag)) return false;
    return true;
  });
  if (rows.length === 0) { console.log("порожньо"); return; }
  for (const e of rows) {
    if (e.broken) { console.log(`  ✗ ${e.file} — ${e.broken}`); continue; }
    const mark = e.meta.status === "superseded" ? "×" : " ";
    const scope = e.meta.scope && e.meta.scope !== "all" ? ` @${e.meta.scope}` : "";
    console.log(`${mark} [${TYPE_LABEL[e.meta.type] ?? e.meta.type}] ${e.meta.id}${scope}`);
    console.log(`    ${e.meta.title}`);
    console.log(`    оновлено ${e.meta.updated}${e.meta.verify ? "  (перевірне)" : ""}`);
  }
}

function cmdShow({ positional }) {
  const id = positional[0] ?? fail("потрібен id");
  const entry = load().find((e) => e.meta?.id === id) ?? fail(`немає запису ${id}`);
  console.log(render(entry.meta, entry.body));
}

function cmdAdd({ positional, flags }) {
  const id = positional[0] ?? fail("потрібен id");
  if (!/^[a-z0-9][a-z0-9-]*$/.test(id)) fail("id: малі літери, цифри й дефіси");
  const existing = load().find((e) => e.meta?.id === id);

  const type = flags.type ?? existing?.meta.type;
  if (!TYPES.includes(type)) fail(`--type має бути один із: ${TYPES.join(", ")}`);
  const title = flags.title ?? existing?.meta.title;
  if (!title) fail("потрібен --title");

  let body = flags.body ?? (existing ? existing.body : "");
  if (flags.why) {
    body = body.replace(/\n*\*\*Чому:\*\*[\s\S]*$/, "").trim();
    body += `\n\n**Чому:** ${flags.why}`;
  }
  if (!body.trim()) fail("потрібен --body (або --why)");

  const meta = {
    id,
    type,
    title,
    scope: flags.scope ?? existing?.meta.scope ?? "all",
    status: "current",
    updated: today(),
    tags: flags.tags ? String(flags.tags).split(",").map((s) => s.trim()) : existing?.meta.tags,
    verify: flags.verify ?? existing?.meta.verify,
    expect: flags.expect ?? existing?.meta.expect,
    supersedes: existing?.meta.supersedes,
  };
  save(id, meta, body);
  console.log(`${existing ? "оновлено" : "створено"}: docs/memory/${id}.md`);
  writeIndex();
}

function cmdSupersede({ positional, flags }) {
  const id = positional[0] ?? fail("потрібен id");
  const by = flags.by ?? fail("потрібен --by <новий-id>");
  const entries = load();
  const entry = entries.find((e) => e.meta?.id === id) ?? fail(`немає запису ${id}`);
  if (!entries.some((e) => e.meta?.id === by)) fail(`немає запису ${by}, спершу створи його`);

  entry.meta.status = "superseded";
  entry.meta.superseded_by = by;
  entry.meta.updated = today();
  const reason = flags.reason ? `\n\n**Скасовано ${today()}:** ${flags.reason}` : "";
  save(id, entry.meta, entry.body + reason);

  const heir = entries.find((e) => e.meta?.id === by);
  const list = Array.isArray(heir.meta.supersedes) ? heir.meta.supersedes : [];
  if (!list.includes(id)) heir.meta.supersedes = [...list, id];
  save(by, heir.meta, heir.body);

  console.log(`${id} → скасовано, спадкоємець ${by}`);
  writeIndex();
}

function runVerify(entry) {
  if (!entry.meta.verify) return { skipped: true };
  try {
    const out = execSync(entry.meta.verify, {
      cwd: ROOT, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], timeout: 30_000,
    }).trim();
    const expect = entry.meta.expect;
    if (expect === undefined) return { ok: true, out };
    return { ok: out.includes(String(expect)), out, expect };
  } catch (error) {
    // Команда впала. Її власний вивід діагностичніший за "Command failed",
    // тож показуємо саме його, і expect лишаємо -- інакше звіт бреше, що
    // ми нічого не очікували.
    const said = String(error.stdout || "").trim() || String(error.stderr || "").trim();
    return { ok: false, out: said || String(error.message).trim(), expect: entry.meta.expect };
  }
}

function cmdVerify({ positional }) {
  const entries = load().filter((e) => !e.broken && e.meta.status !== "superseded");
  const wanted = positional.length ? entries.filter((e) => positional.includes(e.meta.id)) : entries;
  let bad = 0;
  for (const entry of wanted) {
    const result = runVerify(entry);
    if (result.skipped) continue;
    if (result.ok) {
      console.log(`  ✓ ${entry.meta.id}`);
    } else {
      bad++;
      console.log(`  ✗ ${entry.meta.id} — пам'ять розійшлася з дійсністю`);
      console.log(`      очікували: ${result.expect ?? "(успішний код виходу)"}`);
      const said = (result.out || "(порожньо)").split("\n").filter((l) => l.trim()).slice(0, 4);
      console.log(said.map((l, i) => `      ${i ? "           " : "отримали:  "}${l}`).join("\n"));
    }
  }
  if (bad) {
    console.log(`\n${bad} запис(ів) треба оновити: node tools/memory.mjs add <id> --body "..."`);
    process.exitCode = 1;
  }
}

function cmdCheck() {
  const entries = load();
  const ids = new Set();
  let problems = 0;
  const note = (text) => { problems++; console.log(`  ✗ ${text}`); };

  for (const entry of entries) {
    if (entry.broken) { note(`${entry.file}: ${entry.broken}`); continue; }
    const m = entry.meta;
    if (!m.id) { note(`${entry.file}: немає id`); continue; }
    if (`${m.id}.md` !== entry.file) note(`${entry.file}: id ${m.id} не збігається з назвою файлу`);
    if (ids.has(m.id)) note(`${m.id}: дублікат id`);
    ids.add(m.id);
    if (!TYPES.includes(m.type)) note(`${m.id}: невідомий type ${m.type}`);
    if (!m.title) note(`${m.id}: немає title`);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(m.updated ?? "")) note(`${m.id}: updated не дата`);
    if (m.expect && !m.verify) note(`${m.id}: expect без verify`);
  }
  for (const entry of entries) {
    const by = entry.meta?.superseded_by;
    if (by && !ids.has(by)) note(`${entry.meta.id}: superseded_by вказує в нікуди (${by})`);
  }

  const limit = Date.now() - STALE_DAYS * 86400_000;
  for (const entry of entries) {
    if (entry.broken || entry.meta.type !== "state" || entry.meta.status === "superseded") continue;
    if (entry.meta.verify) continue;  // такий запис перевіряє verify, а не годинник
    if (Date.parse(entry.meta.updated) < limit) {
      console.log(`  ! ${entry.meta.id}: стану ${STALE_DAYS}+ днів і немає verify — перечитай`);
    }
  }

  console.log(problems === 0
    ? `\nзаписів: ${entries.length}, помилок немає`
    : `\nпомилок: ${problems}`);
  if (problems) process.exitCode = 1;
}

function writeIndex() {
  const entries = load().filter((e) => !e.broken);
  const live = entries.filter((e) => e.meta.status !== "superseded");
  const out = [
    "# Пам'ять проєкту",
    "",
    "<!-- Згенеровано `node tools/memory.mjs index`. Руками не правити:",
    "     редагуй файли записів або клич `node tools/memory.mjs add`. -->",
    "",
    "Тут те, що інакше губиться при переїзді між машинами: чинний стан,",
    "ухвалені рішення й особливості оточення. Виміряні поведінкові факти про",
    "Live сюди **не** йдуть — їхнє місце в [COVERAGE.md](../COVERAGE.md) і",
    "[PROTOCOL.md](../PROTOCOL.md), як і домовлено в LOCAL-NOTES скіла.",
    "",
  ];
  for (const type of TYPES) {
    const rows = live.filter((e) => e.meta.type === type);
    if (!rows.length) continue;
    out.push(`## ${TYPE_LABEL[type]}`, "");
    for (const e of rows) {
      const scope = e.meta.scope && e.meta.scope !== "all" ? ` *(лише ${e.meta.scope})*` : "";
      out.push(`- [${e.meta.title}](${e.meta.id}.md)${scope} — оновлено ${e.meta.updated}`);
    }
    out.push("");
  }
  const dead = entries.filter((e) => e.meta.status === "superseded");
  if (dead.length) {
    out.push("## скасовані", "");
    for (const e of dead) {
      out.push(`- ~~[${e.meta.title}](${e.meta.id}.md)~~ → \`${e.meta.superseded_by}\``);
    }
    out.push("");
  }
  writeFileSync(INDEX, out.join("\n"), "utf8");
}

// ── точка входу ────────────────────────────────────────────────────────────

const [, , command, ...rest] = process.argv;
const args = parseArgs(rest);
const commands = {
  list: cmdList,
  show: cmdShow,
  add: cmdAdd,
  supersede: cmdSupersede,
  verify: cmdVerify,
  check: cmdCheck,
  index: () => { writeIndex(); console.log("docs/memory/INDEX.md перезібрано"); },
};

if (!command || command === "help" || !commands[command]) {
  const lines = readFileSync(fileURLToPath(import.meta.url), "utf8").split("\n");
  const close = lines.findIndex((l) => l.trim() === "*/");
  console.log(lines.slice(1, close).map((l) => l.replace(/^\s*\*ǀ? ?/, "")).join("\n").trim());
  process.exit(command && command !== "help" ? 1 : 0);
}
commands[command](args);
