/**
 * Фейковий демон для probe-розширення.
 *
 * Робить рівно те, що робитиме справжній daemon, і нічого більше:
 * тримає WebSocket, шле команди, друкує відповіді. Плюс віддає сторінку
 * діалогу по http://localhost — щоб перевірити, чи пускає її WebView2.
 *
 *   node probe-server.mjs
 *
 * Команди зі stdin:
 *   snapshot                       — назви треків і девайсів
 *   insert <index> <name> <device> — вставити стоковий девайс, зі звіркою назви
 *   quit
 */
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { createInterface } from "node:readline";
import { WebSocketServer } from "ws";

const PORT = 19850;
const here = dirname(fileURLToPath(import.meta.url));
const startedAt = Date.now();

const http = createServer(async (request, response) => {
  const url = new URL(request.url ?? "/", `http://localhost:${PORT}`);

  if (url.pathname === "/ping") {
    response.writeHead(200, {
      "content-type": "application/json",
      // Сторінка може приїхати як data: URL — тоді origin у неї null.
      "access-control-allow-origin": "*",
    });
    response.end(JSON.stringify({ ok: true, uptimeSec: uptime() }));
    return;
  }

  if (url.pathname === "/probe.html") {
    try {
      const html = await readFile(join(here, "src", "interface.html"), "utf8");
      response.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      response.end(html);
    } catch (error) {
      response.writeHead(500).end(String(error));
    }
    return;
  }

  response.writeHead(404).end("not found");
});

const wss = new WebSocketServer({ server: http });
let live = null;
let nextId = 1;
const pending = new Map();

wss.on("connection", (socket) => {
  live = socket;
  log("розширення підключилось");

  socket.on("message", (raw) => {
    let message;
    try {
      message = JSON.parse(raw.toString());
    } catch {
      log(`нерозбірливе: ${raw}`);
      return;
    }

    if (message.type === "hello") {
      log(`hello: node=${message.node} platform=${message.platform} uptime=${message.uptimeSec}s`);
      return;
    }
    if (message.type === "alive") {
      log(`alive #${message.beat}, хост живий уже ${message.uptimeSec}s`);
      return;
    }
    if (message.type === "result") {
      const label = pending.get(message.id) ?? `#${message.id}`;
      pending.delete(message.id);
      if (message.ok) {
        log(`${label} → OK\n${JSON.stringify(message.data, null, 2)}`);
      } else {
        log(`${label} → ПОМИЛКА: ${message.error}`);
      }
      return;
    }
    log(`невідоме: ${JSON.stringify(message)}`);
  });

  socket.on("close", () => {
    live = null;
    log("розширення відключилось");
  });
});

function send(command, label) {
  if (live === null || live.readyState !== live.OPEN) {
    log("розширення не підключене");
    return;
  }
  const id = nextId++;
  pending.set(id, label);
  live.send(JSON.stringify({ id, ...command }));
  log(`→ ${label}`);
}

function uptime() {
  return Math.round((Date.now() - startedAt) / 1000);
}

function log(text) {
  const stamp = new Date().toISOString().slice(11, 19);
  process.stdout.write(`[${stamp}] ${text}\n`);
}

http.listen(PORT, "127.0.0.1", () => {
  log(`probe-server на http://localhost:${PORT} (діалог: /probe.html)`);
  log("команди: snapshot | insert <index> <name> <device> | quit");
});

const rl = createInterface({ input: process.stdin });
rl.on("line", (line) => {
  const parts = line.trim().split(/\s+/);
  const verb = parts.shift();

  if (verb === "snapshot") {
    send({ op: "snapshot" }, "snapshot");
  } else if (verb === "insert") {
    const [index, name, ...device] = parts;
    if (index === undefined || name === undefined || device.length === 0) {
      log("формат: insert <index> <track-name> <device name>");
      return;
    }
    send(
      {
        op: "insert_device",
        trackIndex: Number(index),
        trackName: name,
        deviceName: device.join(" "),
        index: 0,
      },
      `insert "${device.join(" ")}" → трек ${index} "${name}"`,
    );
  } else if (verb === "quit") {
    process.exit(0);
  } else if (verb) {
    log(`невідома команда: ${verb}`);
  }
});
