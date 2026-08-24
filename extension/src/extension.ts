/**
 * AbletonMP probe — не шар синхронізації, а чотири питання до живого Live.
 *
 *   1. Чи Extension Host справді живе постійно і чи тримає відкритий WebSocket.
 *   2. Чи вантажиться модалка з http://localhost на Windows (WebView2).
 *   3. Чи працює insertDevice і що саме він приймає як deviceName.
 *   4. Чи витримує звірка «індекс + назва» перерезолвлення handle'ів.
 *
 * Партнером виступає probe-server.mjs — фейковий демон, а не справжній.
 * Нічого не журналює і нічого не синхронізує.
 */
import {
  initialize,
  Track,
  type ActivationContext,
  type Handle,
} from "@ableton-extensions/sdk";

import probeUi from "./interface.html";

const PROBE_URL = "ws://127.0.0.1:19850";
const DIALOG_URL = "http://localhost:19850/probe.html";
const HEARTBEAT_MS = 30_000;
const RECONNECT_MS = 3_000;

type ExtensionApi = ReturnType<typeof initialize<"1.0.0">>;

interface Command {
  id: number;
  op: string;
  trackIndex?: number;
  trackName?: string;
  deviceName?: string;
  index?: number;
}

const startedAt = Date.now();
const uptimeSec = () => Math.round((Date.now() - startedAt) / 1000);

export function activate(activation: ActivationContext) {
  const context = initialize(activation, "1.0.0");

  console.log(
    `[probe] activate: hostApiVersion=${activation.hostApiVersion} node=${process.version} platform=${process.platform}`,
  );
  console.log(
    `[probe] storageDirectory=${context.environment.storageDirectory ?? "(undefined)"}`,
  );
  console.log(
    `[probe] tempDirectory=${context.environment.tempDirectory ?? "(undefined)"}`,
  );

  connect(context);
  registerMenu(context);
}

// ── Питання 1: чи живе хост і чи тримає сокет ──────────────────────────────

function connect(context: ExtensionApi) {
  let socket: WebSocket;
  try {
    socket = new WebSocket(PROBE_URL);
  } catch (error) {
    console.log(`[probe] WebSocket недоступний у хості: ${String(error)}`);
    return;
  }

  let heartbeat: ReturnType<typeof setInterval> | undefined;
  let beat = 0;

  socket.addEventListener("open", () => {
    console.log(`[probe] connected after ${uptimeSec()}s`);
    send(socket, {
      type: "hello",
      node: process.version,
      platform: process.platform,
      uptimeSec: uptimeSec(),
    });
    heartbeat = setInterval(() => {
      beat += 1;
      send(socket, { type: "alive", beat, uptimeSec: uptimeSec() });
    }, HEARTBEAT_MS);
  });

  socket.addEventListener("message", (event: MessageEvent) => {
    void handleCommand(context, socket, String(event.data));
  });

  socket.addEventListener("error", () => {
    console.log("[probe] socket error");
  });

  socket.addEventListener("close", () => {
    if (heartbeat !== undefined) clearInterval(heartbeat);
    console.log(`[probe] disconnected at ${uptimeSec()}s, retry in ${RECONNECT_MS}ms`);
    setTimeout(() => connect(context), RECONNECT_MS);
  });
}

function send(socket: WebSocket, payload: unknown) {
  if (socket.readyState === 1) socket.send(JSON.stringify(payload));
}

// ── Питання 3 і 4: застосування команди ззовні ─────────────────────────────

async function handleCommand(context: ExtensionApi, socket: WebSocket, raw: string) {
  let cmd: Command;
  try {
    cmd = JSON.parse(raw) as Command;
  } catch {
    console.log(`[probe] нерозбірлива команда: ${raw}`);
    return;
  }

  try {
    const data = await runCommand(context, cmd);
    send(socket, { type: "result", id: cmd.id, ok: true, data });
  } catch (error) {
    send(socket, { type: "result", id: cmd.id, ok: false, error: String(error) });
  }
}

async function runCommand(context: ExtensionApi, cmd: Command): Promise<unknown> {
  const song = context.application.song;

  if (cmd.op === "snapshot") {
    return song.tracks.map((track, i) => ({
      index: i,
      name: track.name,
      devices: track.devices.map((device) => device.name),
    }));
  }

  if (cmd.op === "insert_device") {
    const track = resolveTrack(context, cmd.trackIndex, cmd.trackName);
    const deviceName = cmd.deviceName ?? "";
    if (!deviceName) throw new Error("deviceName порожній");
    const device = await track.insertDevice(deviceName, cmd.index ?? 0);
    return { track: track.name, inserted: device.name };
  }

  throw new Error(`невідомий op: ${cmd.op}`);
}

/**
 * Handle'и не переживають переміщення треку — доки прямо кажуть, що переїзд
 * виділяє новий handle. Тому адресуємо щоразу заново, і індекс без назви
 * не приймаємо: якщо партнер переставив треки, ми маємо впасти, а не
 * мовчки застосувати чужу дію не туди.
 */
function resolveTrack(
  context: ExtensionApi,
  index: number | undefined,
  expectedName: string | undefined,
): Track<"1.0.0"> {
  if (index === undefined) throw new Error("trackIndex не заданий");
  const track = context.application.song.tracks[index];
  if (track === undefined) throw new Error(`треку з індексом ${index} немає`);
  if (expectedName !== undefined && track.name !== expectedName) {
    throw new Error(
      `розбіжність ідентичності: індекс ${index} це "${track.name}", очікували "${expectedName}"`,
    );
  }
  return track;
}

// ── Питання 2: модалка з http://localhost ──────────────────────────────────

function registerMenu(context: ExtensionApi) {
  context.commands.registerCommand("abletonmp.probe", (arg: unknown) =>
    void openDialog(context, arg as Handle),
  );

  (["MidiTrack", "AudioTrack"] as const).forEach((scope) => {
    void context.ui
      .registerContextMenuAction(scope, "AbletonMP: probe…", "abletonmp.probe")
      .then(() => console.log(`[probe] context menu registered for ${scope}`))
      .catch((error: unknown) =>
        console.log(`[probe] menu ${scope} failed: ${String(error)}`),
      );
  });
}

async function openDialog(context: ExtensionApi, handle: Handle) {
  const track = context.getObjectFromHandle(handle, Track);
  const index = context.application.song.tracks.findIndex((t) => t.name === track.name);
  console.log(`[probe] menu на треку "${track.name}" (індекс ${index})`);

  // Спершу пробуємо localhost — це і є те, що ми перевіряємо. Якщо WebView2
  // його не пустить, падаємо на data: URL, щоб діалог усе одно відкрився
  // і ми побачили, що саме зламалось.
  let result: string;
  try {
    result = await context.ui.showModalDialog(DIALOG_URL, 560, 420);
    console.log("[probe] http://localhost у модалці працює");
  } catch (error) {
    console.log(`[probe] localhost у модалці НЕ працює: ${String(error)}`);
    result = await context.ui.showModalDialog(
      `data:text/html,${encodeURIComponent(probeUi)}`,
      560,
      420,
    );
    console.log("[probe] data: URL спрацював як запасний варіант");
  }

  const choice = JSON.parse(result) as { deviceName?: string; index?: number };
  if (!choice.deviceName) {
    console.log("[probe] діалог закрито без вибору");
    return;
  }

  const device = await track.insertDevice(choice.deviceName, choice.index ?? 0);
  console.log(`[probe] вставлено "${device.name}" у "${track.name}"`);
}
