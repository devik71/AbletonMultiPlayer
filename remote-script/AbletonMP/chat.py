# -*- coding: utf-8 -*-
"""Authenticated localhost chat/API surface for AI driven Live edits.

The HTTP server runs in a background thread. Every LOM operation is queued back
to the Remote Script and executed from update_display(), which keeps all Live
Object Model access on Live's own thread.
"""

import hmac
import json
import os
import threading
import time
import traceback

try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
except ImportError:
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
    from SocketServer import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

try:
    from urllib.parse import urlparse
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
except ImportError:
    from urlparse import urlparse
    from urllib2 import Request, urlopen, HTTPError, URLError


HOST = "127.0.0.1"
PORT = 19847
MAX_BODY = 512 * 1024
MAX_MESSAGE = 20000
LIVE_TIMEOUT_SEC = 20.0


PLAN_CONTRACT = {
    "reply": "Short user-facing text.",
    "needs_confirmation": False,
    "actions": [
        {
            "op": "set_tempo",
            "bpm": 128,
        },
        {
            "op": "create_track",
            "kind": "midi",
            "index": 0,
            "name": "Bass",
        },
        {
            "op": "set_mixer",
            "track_index": 0,
            "param": "volume",
            "value": 0.75,
        },
        {
            "op": "lom_set",
            "path": ["tracks", 0],
            "property": "name",
            "value": "Lead",
        },
        {
            "op": "lom_call",
            "path": ["scenes", 0],
            "method": "fire",
            "args": [],
            "kwargs": {},
        },
    ],
}

# Другий приклад -- для запиту, який одним блоком не робиться. Контракт
# показує саме залежність: девайс кладеться на трек, якого до першого
# блоку не існувало, тож між блоками знімок перезнімається.
PLAN_CONTRACT_STAGED = {
    "reply": "Three tracks, then instruments, then a rough balance.",
    "needs_confirmation": False,
    "stages": [
        {
            "title": "create tracks",
            "actions": [
                {"op": "create_track", "kind": "midi", "name": "Drums"},
                {"op": "create_track", "kind": "midi", "name": "Bass"},
            ],
        },
        {
            "title": "load instruments",
            "actions": [
                {"op": "load_device", "track_index": 0, "name": "Drum Rack"},
                {"op": "load_device", "track_index": 1, "name": "Operator"},
            ],
        },
        {
            "title": "rough balance",
            "actions": [
                {"op": "set_mixer", "track_index": 0, "param": "volume", "value": 0.8},
                {"op": "set_mixer", "track_index": 1, "param": "volume", "value": 0.7},
            ],
        },
    ],
}

PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reply": {"type": "string"},
        "needs_confirmation": {"type": "boolean"},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": True,
            },
        },
        # Складний запит розкладається на залежні блоки: спершу створити
        # треки, потім покласти на них девайси, потім крутити параметри.
        # Блок -- це одиниця виконання, а не оформлення: між блоками
        # знімок перезнімається, тож наступний адресує те, що щойно
        # створив попередній.
        "stages": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "title": {"type": "string"},
                    "needs_confirmation": {"type": "boolean"},
                    "actions": {
                        "type": "array",
                        "items": {"type": "object", "additionalProperties": True},
                    },
                },
            },
        },
    },
    "required": ["reply", "needs_confirmation", "actions"],
}

# Скільки блоків має сенс. Один -- це не план, а дія; понад десять означає,
# що модель дрібнить замість планувати, і кожен зайвий блок -- це ще один
# похід у Live з перезніманням знімка.
MAX_STAGES = 10

# Коди, після яких наступна спроба безглузда: ключ, доступ і неіснуюча модель
# не полагодяться від іншого формату запиту.
CONFIG_HTTP_CODES = (401, 403, 404)
# Менше секунди на спробу -- це вже не спроба, а гарантований таймаут.
MIN_ATTEMPT_SEC = 2.0


class _ConfigError(RuntimeError):
    """Помилка налаштування, а не формату: повторювати не варто."""

ACTION_HELP = {
    "contract": PLAN_CONTRACT,
    "staged_contract": PLAN_CONTRACT_STAGED,
    "ops": [
        "snapshot",
        "apply",
        "transport",
        "set_tempo",
        "create_track",
        "delete_track",
        "rename_track",
        "set_track_color",
        "set_track_toggle",
        "set_mixer",
        "create_scene",
        "delete_scene",
        "rename_scene",
        "launch_scene",
        "launch_clip",
        "stop_clip",
        "stop_all_clips",
        "create_midi_clip",
        "delete_clip",
        "replace_clip_notes",
        "set_device_parameter",
        "load_device",
        "lom_get",
        "lom_set",
        "lom_call",
    ],
    "lom_path": [
        "Paths start at Song by default, for example ['tracks', 0, 'mixer_device', 'volume'].",
        "Use ['app'] to start at Live.Application, or ['song'] to be explicit.",
        "Private attributes and methods beginning with '_' are rejected.",
    ],
}


SYSTEM_PROMPT = """You control Ableton Live through an authenticated localhost bridge.
Return only a JSON object matching this shape:
{"reply":"short text","needs_confirmation":false,"actions":[{"op":"..."}]}

Use high-level ops when possible. Use generic lom_get/lom_set/lom_call only when
the request cannot be expressed by a high-level op. Paths address the Live Object
Model from Song by default, e.g. ["tracks",0,"mixer_device","volume"].

For a request that needs several dependent steps, return "stages": an ordered
list of blocks, each with "title" and "actions". A block is a unit of execution:
the snapshot is re-read between blocks, so a later block may address objects an
earlier one created. Use at most 10 blocks, and put a block that needs a human
decision behind its own "needs_confirmation": true instead of guessing.

Keep actions small and reversible where practical. Do not attempt filesystem,
network, shell, plugin installation, or arbitrary Python access. Use only LOM
operations represented in JSON actions. If the user's request is ambiguous or
destructive, set needs_confirmation=true and return the proposed actions without
assuming missing details.
"""


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AbletonMP AI</title>
<style>
:root {
  color-scheme: dark;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: #111513;
  color: #e9f0ed;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  min-height: 100vh;
  background: #111513;
}
.app {
  min-height: 100vh;
  display: grid;
  grid-template-rows: auto 1fr auto;
}
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 16px;
  border-bottom: 1px solid #2b3430;
  background: #171d1a;
}
h1 {
  margin: 0;
  font-size: 16px;
  font-weight: 650;
}
.auth {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 280px;
}
input, textarea, button {
  font: inherit;
}
input, textarea {
  width: 100%;
  border: 1px solid #3b4641;
  background: #0d100f;
  color: #e9f0ed;
  border-radius: 6px;
  outline: none;
}
input {
  height: 32px;
  padding: 0 10px;
}
textarea {
  min-height: 84px;
  max-height: 32vh;
  resize: vertical;
  padding: 10px;
  line-height: 1.35;
}
button {
  height: 34px;
  padding: 0 12px;
  border: 1px solid #476256;
  border-radius: 6px;
  color: #f6fffb;
  background: #245b47;
  cursor: pointer;
  white-space: nowrap;
}
button.secondary {
  background: #202823;
  border-color: #3b4641;
}
button:disabled {
  opacity: .5;
  cursor: wait;
}
main {
  overflow: auto;
  padding: 16px;
}
.messages {
  max-width: 980px;
  margin: 0 auto;
  display: grid;
  gap: 10px;
}
.msg {
  border: 1px solid #2b3430;
  background: #171d1a;
  border-radius: 8px;
  padding: 10px 12px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.msg.user {
  border-color: #375d50;
  background: #14241e;
}
.msg.error {
  border-color: #79433f;
  background: #2c1715;
}
pre {
  margin: 8px 0 0;
  padding: 10px;
  overflow: auto;
  max-height: 36vh;
  border-radius: 6px;
  background: #0d100f;
  border: 1px solid #2b3430;
}
footer {
  border-top: 1px solid #2b3430;
  padding: 12px 16px;
  background: #171d1a;
}
.composer {
  max-width: 980px;
  margin: 0 auto;
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 10px;
  align-items: end;
}
.controls {
  display: flex;
  gap: 8px;
}
.toggle {
  display: flex;
  align-items: center;
  gap: 7px;
  color: #b7c5bf;
  font-size: 13px;
}
.toggle input {
  width: 16px;
  height: 16px;
}
@media (max-width: 720px) {
  header { align-items: stretch; flex-direction: column; }
  .auth { min-width: 0; }
  .composer { grid-template-columns: 1fr; }
  .controls { justify-content: space-between; flex-wrap: wrap; }
}
</style>
</head>
<body>
<div class="app">
  <header>
    <h1>AbletonMP AI</h1>
    <div class="auth">
      <input id="token" type="password" autocomplete="current-password" placeholder="Token">
      <button id="saveToken" class="secondary">Auth</button>
    </div>
  </header>
  <main><div id="messages" class="messages"></div></main>
  <footer>
    <div class="composer">
      <textarea id="message" placeholder="Ask for an edit in Live"></textarea>
      <div class="controls">
        <label class="toggle"><input id="execute" type="checkbox" checked>Run</label>
        <button id="snapshot" class="secondary">Snapshot</button>
        <button id="send">Send</button>
      </div>
    </div>
  </footer>
</div>
<script>
const $ = (id) => document.getElementById(id);
const messages = $("messages");
const tokenInput = $("token");
tokenInput.value = localStorage.getItem("abletonmp_token") || "";

const STATUS_TEXT = {ok: "done", failed: "failed", confirm: "needs confirmation"};

// Дії, які ще НЕ виконались. Плоский actions -- це блоки підряд, тож
// пропустити треба рівно стільки, скільки їх у блоках зі статусом ok.
// Без цього кнопка переганяла б успішні блоки ще раз.
function pendingActions(extra) {
  if (!extra || !Array.isArray(extra.actions) || !extra.actions.length) return [];
  const stages = Array.isArray(extra.stages) ? extra.stages : [];
  const progress = Array.isArray(extra.progress) ? extra.progress : [];
  if (!stages.length || !progress.length) return extra.executed ? [] : extra.actions;
  let done = 0;
  for (let i = 0; i < progress.length && i < stages.length; i++) {
    if (progress[i].status !== "ok") break;
    done += Number(stages[i].actions) || 0;
  }
  return extra.actions.slice(done);
}

function stageSummary(extra) {
  const stages = Array.isArray(extra && extra.stages) ? extra.stages : [];
  if (stages.length < 2) return null;
  const progress = Array.isArray(extra.progress) ? extra.progress : [];
  const lines = stages.map((stage, i) => {
    const done = progress[i];
    const status = done ? (STATUS_TEXT[done.status] || done.status) : "not started";
    const title = stage.title || ("block " + (i + 1));
    return (i + 1) + ". " + title + " — " + status + " (" + (stage.actions || 0) + ")";
  });
  return lines.join("\n");
}

function add(role, text, extra, isError) {
  const el = document.createElement("div");
  el.className = "msg " + role + (isError ? " error" : "");
  el.textContent = text || "";
  if (extra !== undefined) {
    // Підсумок по блоках -- окремо й першим: у сирому JSON його не видно
    // серед знімка й результатів, а це найголовніше в довгому плані.
    const summary = stageSummary(extra);
    if (summary) {
      const head = document.createElement("pre");
      head.textContent = summary;
      head.style.opacity = "0.85";
      el.appendChild(head);
    }
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(extra, null, 2);
    el.appendChild(pre);
    const pending = pendingActions(extra);
    if (pending.length) {
      const run = document.createElement("button");
      run.textContent = pending.length === (extra.actions || []).length
        ? "Run actions"
        : "Run remaining " + pending.length;
      run.style.marginTop = "8px";
      run.onclick = async () => {
        run.disabled = true;
        try {
          const result = await api("/api/exec", {actions: pending});
          add("assistant", "Executed", result);
        } catch (err) {
          add("assistant", err.message, undefined, true);
        } finally {
          run.disabled = false;
        }
      };
      el.appendChild(run);
    }
  }
  messages.appendChild(el);
  el.scrollIntoView({block: "end"});
}

function headers() {
  return {
    "Content-Type": "application/json",
    "X-AbletonMP-Token": tokenInput.value
  };
}

async function api(path, body) {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: headers(),
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || ("HTTP " + res.status));
  }
  return data;
}

$("saveToken").onclick = async () => {
  localStorage.setItem("abletonmp_token", tokenInput.value);
  try {
    const status = await api("/api/status");
    add("assistant", "Authenticated", status);
  } catch (err) {
    add("assistant", err.message, undefined, true);
  }
};

$("snapshot").onclick = async () => {
  try {
    add("user", "Snapshot");
    const data = await api("/api/snapshot");
    add("assistant", "Current Live state", data.snapshot);
  } catch (err) {
    add("assistant", err.message, undefined, true);
  }
};

$("send").onclick = async () => {
  const text = $("message").value.trim();
  if (!text) return;
  $("message").value = "";
  $("send").disabled = true;
  add("user", text);
  try {
    const data = await api("/api/chat", {message: text, execute: $("execute").checked});
    add("assistant", data.reply || "Done", data);
  } catch (err) {
    add("assistant", err.message, undefined, true);
  } finally {
    $("send").disabled = false;
  }
};

$("message").addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
    $("send").click();
  }
});
</script>
</body>
</html>
"""


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except Exception:
        return default


def _json(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _state_dir():
    candidates = [os.environ.get("ABLETONMP_HOME")]
    home = os.path.expanduser("~")
    if home and home != "~":
        candidates.append(os.path.join(home, ".abletonmp"))
    for base in (os.environ.get("APPDATA"), os.environ.get("TMPDIR")):
        if base:
            candidates.append(os.path.join(base, "AbletonMP"))
    candidates.append(".")
    for path in candidates:
        if not path:
            continue
        try:
            if not os.path.isdir(path):
                os.makedirs(path)
            return path
        except Exception:
            continue
    return "."


def _new_token():
    try:
        return os.urandom(24).hex()
    except Exception:
        return ("%f-%d" % (time.time(), os.getpid())).replace(".", "")


def _load_token(log):
    env = os.environ.get("ABLETONMP_CHAT_TOKEN")
    if env:
        return env, "ABLETONMP_CHAT_TOKEN"
    path = os.path.join(_state_dir(), "chat_token")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                token = f.read().strip()
            if token:
                return token, path
    except Exception:
        pass
    token = _new_token()
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(token + "\n")
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return token, path
    except Exception as e:
        log("AI chat token is in memory only: %r" % (e,))
        return token, "(memory)"


def _load_api_key():
    for name in ("ABLETONMP_AI_API_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(name)
        if value:
            return value.strip(), name
    paths = []
    configured = os.environ.get("ABLETONMP_AI_KEY_FILE")
    if configured:
        paths.append(configured)
    try:
        paths.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "openai_api_key"))
    except Exception:
        pass
    paths.append("/Users/macbook/Desktop/AbletonMultiPlayer-main/remote-script/AbletonMP/openai_api_key")
    paths.append(os.path.join(_state_dir(), "openai_api_key"))
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                value = f.read().strip()
            if value:
                return value, path
        except Exception:
            pass
    return None, None


def _extract_json_object(text):
    text = (text or "").strip()
    if not text:
        raise ValueError("empty model response")
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _safe_remote_error(text):
    text = text or ""
    markers = (
        "Incorrect API key provided:",
        "You can find your API key",
    )
    if any(marker in text for marker in markers):
        return "OpenAI rejected the configured API key (invalid_api_key)."
    return text[:1000]


class LiveRequest(object):
    def __init__(self, command, payload):
        self.command = command
        self.payload = payload
        self.event = threading.Event()
        self.response = None
        self.error = None


class OpenAIPlanner(object):
    def __init__(self, log):
        self._log = log
        self.api_key, self.api_key_source = _load_api_key()
        self.model = os.environ.get("ABLETONMP_AI_MODEL", "gpt-5")
        self.base_url = os.environ.get("ABLETONMP_AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.timeout = _env_int("ABLETONMP_AI_TIMEOUT", 45)
        self._deadline = None

    def _refresh_api_key(self):
        api_key, source = _load_api_key()
        if api_key:
            self.api_key = api_key
            self.api_key_source = source

    @property
    def ready(self):
        self._refresh_api_key()
        return bool(self.api_key)

    def status(self):
        return {
            "ready": self.ready,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_source": self.api_key_source if self.ready else None,
        }

    def plan(self, message, snapshot):
        self._refresh_api_key()
        if not self.ready:
            parsed = _plan_from_text(message)
            if parsed is not None:
                return parsed
            return {
                "reply": "OpenAI API key is not set. Paste a JSON plan, set OPENAI_API_KEY, or put the key in ~/.abletonmp/openai_api_key before using natural-language AI planning.",
                "needs_confirmation": True,
                "actions": [],
            }

        user_payload = {
            "request": message,
            "live_snapshot": snapshot,
            "action_contract": PLAN_CONTRACT,
            "staged_contract": PLAN_CONTRACT_STAGED,
            "available_actions": ACTION_HELP["ops"],
        }
        prompt = json.dumps(user_payload, ensure_ascii=False)
        # Спільний строк на ВСІ спроби, а не по строку на кожну. Три
        # послідовні падіння по 45 секунд давали 135 секунд тиші на один
        # запит -- людина за цей час устигала вирішити, що все зламалось.
        self._deadline = time.time() + self.timeout
        attempts = (
            ("responses", lambda: self._responses_plan(prompt)),
            ("chat-json", lambda: self._chat_plan(prompt, json_mode=True)),
            ("chat", lambda: self._chat_plan(prompt, json_mode=False)),
        )
        errors = []
        try:
            for i, (name, attempt) in enumerate(attempts):
                if i and self._time_left() < MIN_ATTEMPT_SEC:
                    errors.append("%s: пропущено, часу не лишилось" % (name,))
                    break
                try:
                    return attempt()
                except _ConfigError as e:
                    # 401/403/404 -- це не збій формату, а налаштування.
                    # Наступна спроба піде тим самим ключем у ту саму модель
                    # і впаде так само, лише витративши решту строку.
                    errors.append("%s: %s" % (name, e))
                    break
                except Exception as e:
                    errors.append("%s: %r" % (name, e))
                    self._log("AI planning attempt %s failed: %r" % (name, e))
            raise RuntimeError("; ".join(errors))
        finally:
            self._deadline = None

    def _time_left(self):
        if self._deadline is None:
            return self.timeout
        return self._deadline - time.time()

    def _attempt_timeout(self):
        """Скільки лишилось на цю спробу. Не менше секунди -- нульовий
        таймаут у urlopen означає не «одразу здатись», а «чекати вічно»."""
        return max(1.0, min(float(self.timeout), self._time_left()))

    def _post(self, path, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        req = Request(self.base_url + path, data=data, headers=headers)
        try:
            response = urlopen(req, timeout=self._attempt_timeout())
            raw = response.read().decode("utf-8")
        except HTTPError as e:
            try:
                raw = e.read().decode("utf-8")
            except Exception:
                raw = str(e)
            code = getattr(e, "code", "?")
            text = "HTTP %s %s" % (code, _safe_remote_error(raw))
            if code in CONFIG_HTTP_CODES:
                raise _ConfigError(text)
            raise RuntimeError(text)
        except URLError as e:
            raise RuntimeError("network error %r" % (e,))
        return json.loads(raw)

    def _responses_plan(self, prompt):
        payload = {
            "model": self.model,
            "instructions": SYSTEM_PROMPT,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "abletonmp_ai_plan",
                    "schema": PLAN_SCHEMA,
                    "strict": False,
                }
            },
        }
        data = self._post("/responses", payload)
        text = data.get("output_text") or self._responses_text(data)
        return _normalize_plan(_extract_json_object(text))

    def _responses_text(self, data):
        chunks = []
        for item in data.get("output") or []:
            for content in item.get("content") or []:
                if isinstance(content, dict):
                    text = content.get("text")
                    if text:
                        chunks.append(text)
        return "\n".join(chunks)

    def _chat_plan(self, prompt, json_mode):
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = self._post("/chat/completions", payload)
        text = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        return _normalize_plan(_extract_json_object(text))


def _plan_from_text(text):
    try:
        parsed = _extract_json_object(text)
    except Exception:
        return None
    try:
        return _normalize_plan(parsed)
    except Exception:
        return None


def _normalize_plan(plan):
    if isinstance(plan, list):
        plan = {"reply": "Plan parsed.", "actions": plan}
    if not isinstance(plan, dict):
        raise ValueError("plan must be an object")
    actions = plan.get("actions")
    if actions is None:
        single = plan.get("action")
        actions = [single] if isinstance(single, dict) else []
    if not isinstance(actions, list):
        raise ValueError("plan.actions must be a list")
    normal = []
    for action in actions:
        if not isinstance(action, dict):
            raise ValueError("each action must be an object")
        normal.append(action)
    stages = _normalize_stages(plan.get("stages"), normal)
    # actions лишається ПЛОСКИМ переліком усього запланованого: на нього
    # дивиться і M4L-панель, і /api/exec, і вони не мусять знати про блоки.
    flat = []
    for stage in stages:
        flat.extend(stage["actions"])
    reply = plan.get("reply")
    if not isinstance(reply, str):
        reply = "Plan ready." if flat else "No actions."
    return {
        "reply": reply,
        "needs_confirmation": bool(plan.get("needs_confirmation", False)),
        "actions": flat,
        "stages": stages,
    }


def _normalize_stages(raw, fallback_actions):
    """Блоки виконання. Порожній перелік -- коли й дій немає.

    План без blocks -- це один блок: так старий формат лишається робочим
    без жодної гілки в місці виконання.
    """
    if not isinstance(raw, list) or not raw:
        return [{"title": "", "needs_confirmation": False,
                 "actions": fallback_actions}] if fallback_actions else []
    if len(raw) > MAX_STAGES:
        raise ValueError("plan.stages must not exceed %d blocks" % MAX_STAGES)
    stages = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each stage must be an object")
        actions = item.get("actions")
        if not isinstance(actions, list):
            raise ValueError("stage.actions must be a list")
        for action in actions:
            if not isinstance(action, dict):
                raise ValueError("each action must be an object")
        title = item.get("title")
        stages.append({
            "title": title if isinstance(title, str) else "",
            "needs_confirmation": bool(item.get("needs_confirmation", False)),
            "actions": list(actions),
        })
    # Блок без дій нічого не виконує, але й не має права зупиняти чергу.
    return [stage for stage in stages if stage["actions"]]


class AIChatServer(object):
    def __init__(self, log, host=None, port=None):
        self._log = log
        self.host = host or os.environ.get("ABLETONMP_CHAT_HOST", HOST)
        self.port = port or _env_int("ABLETONMP_CHAT_PORT", PORT)
        self._token, self.token_source = _load_token(log)
        self._planner = OpenAIPlanner(log)
        self._requests = []
        self._lock = threading.Lock()
        self._httpd = None
        self._thread = None
        self.url = None

    @property
    def alive(self):
        return self._httpd is not None

    @property
    def token(self):
        return self._token

    def start(self):
        handler = self._make_handler()
        last_error = None
        for candidate in range(self.port, self.port + 10):
            try:
                httpd = ThreadingHTTPServer((self.host, candidate), handler)
                try:
                    httpd.daemon_threads = True
                except Exception:
                    pass
                self.port = candidate
                self._httpd = httpd
                self.url = "http://%s:%d/" % (self.host, self.port)
                break
            except Exception as e:
                last_error = e
        if self._httpd is None:
            self._log("AI chat server failed to bind: %r" % (last_error,))
            return False
        self._thread = threading.Thread(target=self._serve, name="AbletonMP AI Chat")
        self._thread.daemon = True
        self._thread.start()
        self._log("AI chat listening on %s token=%s" % (self.url, self.token_source))
        return True

    def stop(self):
        httpd = self._httpd
        self._httpd = None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None:
            try:
                thread.join(1.0)
            except Exception:
                pass

    def poll(self, handler, max_requests=16):
        batch = []
        with self._lock:
            while self._requests and len(batch) < max_requests:
                batch.append(self._requests.pop(0))
        for req in batch:
            try:
                req.response = handler(req.command, req.payload)
            except Exception:
                req.error = traceback.format_exc()
            finally:
                req.event.set()

    def live_request(self, command, payload, timeout=LIVE_TIMEOUT_SEC):
        req = LiveRequest(command, payload)
        with self._lock:
            self._requests.append(req)
        if not req.event.wait(timeout):
            raise RuntimeError("Live did not answer within %.1fs" % timeout)
        if req.error:
            raise RuntimeError(req.error)
        return req.response

    def run_stages(self, plan, message, execute=True):
        """Виконує блоки по черзі. Повертає (прогрес, останній результат).

        Знімок між блоками перезнімається не для краси: другий блок кладе
        девайс на трек, якого до першого блоку не існувало, і без свіжого
        знімка адресувати його нічим.

        Черга зупиняється на першій же невдачі, а не доводить решту до
        кінця: наступні блоки залежні за побудовою, і виконати їх поверх
        напівзробленого -- це отримати сет, якого ніхто не просив.
        """
        stages = plan.get("stages") or []
        progress = []
        last = None
        if not execute or not stages:
            return progress, None
        if plan.get("needs_confirmation"):
            for i, stage in enumerate(stages):
                progress.append({"stage": i, "title": stage["title"],
                                 "status": "confirm", "actions": len(stage["actions"])})
            return progress, None
        for i, stage in enumerate(stages):
            if stage.get("needs_confirmation"):
                progress.append({"stage": i, "title": stage["title"],
                                 "status": "confirm", "actions": len(stage["actions"])})
                break
            try:
                result = self.live_request("exec", {
                    "actions": stage["actions"],
                    "source": "chat",
                    "message": message,
                })
            except Exception as e:
                progress.append({"stage": i, "title": stage["title"],
                                 "status": "failed", "actions": len(stage["actions"]),
                                 "error": str(e)})
                break
            last = result
            ok = bool(result.get("ok", True)) if isinstance(result, dict) else True
            progress.append({"stage": i, "title": stage["title"],
                             "status": "ok" if ok else "failed",
                             "actions": len(stage["actions"]),
                             "result": result})
            if not ok:
                break
        return progress, last

    def _serve(self):
        try:
            self._httpd.serve_forever()
        except Exception as e:
            self._log("AI chat server stopped with error: %r" % (e,))

    def _make_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "AbletonMPAI/1.0"

            def log_message(self, fmt, *args):
                return

            def do_OPTIONS(self):
                self.send_response(204)
                self._cors()
                self.end_headers()

            def do_GET(self):
                path = urlparse(self.path).path
                if path in ("/", "/index.html"):
                    self._send_html(INDEX_HTML)
                    return
                if path == "/api/status":
                    if not self._require_auth():
                        return
                    self._send_json({
                        "ok": True,
                        "url": outer.url,
                        "token_source": outer.token_source,
                        "ai": outer._planner.status(),
                    })
                    return
                if path == "/api/schema":
                    if not self._require_auth():
                        return
                    self._send_json({"ok": True, "schema": ACTION_HELP})
                    return
                if path == "/api/snapshot":
                    if not self._require_auth():
                        return
                    try:
                        snapshot = outer.live_request("snapshot", {})
                        self._send_json({"ok": True, "snapshot": snapshot})
                    except Exception as e:
                        self._send_json({"ok": False, "error": str(e)}, status=500)
                    return
                self._send_json({"ok": False, "error": "not found"}, status=404)

            def do_POST(self):
                path = urlparse(self.path).path
                if not self._require_auth():
                    return
                try:
                    data = self._read_json()
                except Exception as e:
                    self._send_json({"ok": False, "error": str(e)}, status=400)
                    return
                if path == "/api/chat":
                    self._post_chat(data)
                    return
                if path == "/api/exec":
                    self._post_exec(data)
                    return
                self._send_json({"ok": False, "error": "not found"}, status=404)

            def _post_chat(self, data):
                message = data.get("message")
                if not isinstance(message, str) or not message.strip():
                    self._send_json({"ok": False, "error": "message is required"}, status=400)
                    return
                if len(message) > MAX_MESSAGE:
                    self._send_json({"ok": False, "error": "message is too long"}, status=413)
                    return
                execute = bool(data.get("execute", True))
                try:
                    snapshot = outer.live_request("snapshot", {})
                    plan = outer._planner.plan(message, snapshot)
                    progress, result = outer.run_stages(plan, message, execute)
                    self._send_json({
                        "ok": True,
                        "reply": plan.get("reply"),
                        "needs_confirmation": bool(plan.get("needs_confirmation")),
                        "actions": plan.get("actions") or [],
                        "stages": [{"title": st["title"],
                                    "actions": len(st["actions"])}
                                   for st in plan.get("stages") or []],
                        "progress": progress,
                        "executed": result is not None,
                        "result": result,
                    })
                except Exception as e:
                    self._send_json({"ok": False, "error": str(e)}, status=500)

            def _post_exec(self, data):
                plan = data.get("plan") if isinstance(data.get("plan"), dict) else data
                actions = plan.get("actions") if isinstance(plan, dict) else None
                if actions is None and isinstance(plan, list):
                    actions = plan
                try:
                    norm = _normalize_plan({"actions": actions or []})
                    result = outer.live_request("exec", {
                        "actions": norm["actions"],
                        "source": "manual",
                    })
                    self._send_json({"ok": True, "result": result})
                except Exception as e:
                    self._send_json({"ok": False, "error": str(e)}, status=400)

            def _read_json(self):
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length <= 0:
                    return {}
                if length > MAX_BODY:
                    raise ValueError("request body is too large")
                raw = self.rfile.read(length).decode("utf-8")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("JSON body must be an object")
                return data

            def _require_auth(self):
                provided = self.headers.get("X-AbletonMP-Token") or ""
                auth = self.headers.get("Authorization") or ""
                if auth.lower().startswith("bearer "):
                    provided = auth[7:].strip()
                if provided and hmac.compare_digest(provided, outer.token):
                    return True
                self._send_json({"ok": False, "error": "unauthorized"}, status=401)
                return False

            def _send_html(self, body, status=200):
                data = body.encode("utf-8")
                self.send_response(status)
                self._cors()
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send_json(self, obj, status=200):
                data = _json(obj)
                self.send_response(status)
                self._cors()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:%d" % outer.port)
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-AbletonMP-Token, Authorization")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

        return Handler
