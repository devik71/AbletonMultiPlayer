autowatch = 1;
inlets = 1;
outlets = 3;

var relayUrl = "http://127.0.0.1:19870";
var sessionFilter = "";
var relayToken = "";
var pollMs = 2000;
var timer = null;
var busy = 0;
var lastHead = {};

function loadbang() {
    start();
}

function seturl() {
    relayUrl = trim(arrayfromargs(arguments).join(" ")).replace(/\/+$/, "");
    if (!relayUrl) {
        relayUrl = "http://127.0.0.1:19870";
    }
    setstatus("Relay " + relayUrl);
    refresh();
}

function token() {
    // Потрібен лише якщо relay піднятий із MP_RELAY_TOKEN: без нього /health
    // віддає 401. Порожній рядок прибирає токен назад.
    relayToken = trim(arrayfromargs(arguments).join(" "));
    setstatus(relayToken ? "Token set" : "Token cleared");
    refresh();
}

function session() {
    sessionFilter = trim(arrayfromargs(arguments).join(" "));
    refresh();
}

function interval(v) {
    pollMs = Math.max(500, Number(v) || 2000);
    if (timer) {
        stop();
        start();
    }
}

function start() {
    if (timer) {
        return;
    }
    setstatus("Watching " + relayUrl);
    refresh();
    timer = new Task(tick, this);
    timer.interval = pollMs;
    timer.repeat();
}

function stop() {
    if (timer) {
        timer.cancel();
        timer = null;
    }
    setstatus("Paused");
}

function tick() {
    refresh();
}

function refresh() {
    if (busy) {
        return;
    }
    busy = 1;
    try {
        var xhr = new XMLHttpRequest();
        xhr.onreadystatechange = function () {
            if (xhr.readyState !== 4) {
                return;
            }
            busy = 0;
            if (xhr.status === 401) {
                setstatus("Token required");
                setbody("relay піднятий із MP_RELAY_TOKEN: надішли той самий рядок повідомленням token");
                return;
            }
            if (xhr.status < 200 || xhr.status >= 300) {
                setstatus("Relay offline");
                setbody("HTTP " + xhr.status + "\n" + (xhr.responseText || ""));
                return;
            }
            var data;
            try {
                data = JSON.parse(xhr.responseText || "{}");
            } catch (e) {
                setstatus("Bad JSON");
                setbody(String(e));
                return;
            }
            render(data);
        };
        var url = relayUrl + "/health";
        if (relayToken) {
            url += "?token=" + encodeURIComponent(relayToken);
        }
        xhr.open("GET", url, true);
        xhr.send();
    } catch (e) {
        busy = 0;
        setstatus("Request error");
        setbody(String(e));
    }
}

/** Однакова версія скрипта з різним хешем коду. Повертає опис або "". */
function codeDrift(clients) {
    var byScript = {};
    for (var i = 0; i < clients.length; i++) {
        var c = clients[i];
        if (!c.script || !c.sha) {
            continue;
        }
        if (!byScript[c.script]) {
            byScript[c.script] = {};
        }
        byScript[c.script][c.sha] = (byScript[c.script][c.sha] || []).concat(c.author);
    }
    for (var script in byScript) {
        if (!byScript.hasOwnProperty(script)) {
            continue;
        }
        var shas = [];
        for (var sha in byScript[script]) {
            if (byScript[script].hasOwnProperty(sha)) {
                shas.push(sha + "=" + byScript[script][sha].join(","));
            }
        }
        if (shas.length > 1) {
            return script + ": " + shas.join("  ");
        }
    }
    return "";
}

function render(data) {
    if (!data || data.ok === false) {
        setstatus("Relay error");
        setbody(stringify(data));
        return;
    }
    var sessions = data.sessions || [];
    if (sessionFilter) {
        sessions = sessions.filter(function (s) { return s.session === sessionFilter; });
    }
    var totalOnline = 0;
    var totalActions = 0;
    for (var i = 0; i < sessions.length; i++) {
        totalOnline += Number(sessions[i].online || 0);
        var authors = sessions[i].authors || [];
        for (var a = 0; a < authors.length; a++) {
            totalActions += Number(authors[a].actions || 0);
        }
    }
    setstatus("Relay OK  rooms " + sessions.length + "  players " + totalOnline + "  actions " + totalActions);

    var lines = [];
    lines.push("Relay: " + relayUrl + "   proto " + data.proto);
    lines.push("Updated: " + nowTime());
    lines.push("");
    if (!sessions.length) {
        lines.push(sessionFilter ? ("No session named " + sessionFilter) : "No active sessions yet.");
        setbody(lines.join("\n"));
        return;
    }

    for (var sidx = 0; sidx < sessions.length; sidx++) {
        var s = sessions[sidx];
        var key = s.session;
        var prevHead = lastHead[key] || 0;
        var delta = Number(s.head || 0) - prevHead;
        lastHead[key] = Number(s.head || 0);
        lines.push("ROOM " + s.session);
        lines.push("  server: " + healthText(s) + "   head #" + (s.head || 0) +
            (delta > 0 ? ("  +" + delta + " since last poll") : ""));
        lines.push("  online: " + (s.online || 0) + "   peers: " + ((s.peers || []).join(", ") || "-"));

        var clients = s.clients || [];
        if (clients.length) {
            lines.push("  players:");
            for (var cidx = 0; cidx < clients.length; cidx++) {
                var c = clients[cidx];
                lines.push("    " + c.author + "  " + (c.ip || "?") +
                    (c.port ? (":" + c.port) : "") +
                    "  live " + (c.live || "?") +
                    "  script " + (c.script || "?") +
                    (c.sha ? ("#" + c.sha) : "") +
                    "  connected " + seconds(c.connected_sec || 0) +
                    "  idle " + seconds(c.idle_sec || 0));
            }
            // Версія між комітами не змінюється, тож однакова версія при
            // різному хеші -- це саме той випадок, коли "синхронізація не
            // працює" означає "одна машина крутить старий скрипт". Найдорожча
            // година прогону витрачається саме на нього, тож кажемо вголос.
            var drift = codeDrift(clients);
            if (drift) {
                lines.push("  !! РІЗНИЙ КОД при однаковій версії " + drift);
                lines.push("     онови скрипт на машині, що відстала, і перезапусти Live");
            }
        }

        var locks = s.locks || [];
        if (locks.length) {
            lines.push("  editing now:");
            for (var kidx = 0; kidx < locks.length; kidx++) {
                var lk = locks[kidx];
                lines.push("    " + lk.author + "  " + (lk.label || lk.object) +
                    "  " + seconds(lk.held_sec || 0));
            }
        }

        var watching = s.presence || [];
        if (watching.length) {
            lines.push("  looking at:");
            for (var widx = 0; widx < watching.length; widx++) {
                var w = watching[widx];
                var view = w.view || {};
                var names = view.names || {};
                var where = [names.track, names.scene].filter(function (v) { return !!v; }).join(" / ");
                lines.push("    " + w.author + "  " + (where || "—") +
                    (view.screen ? ("  [" + view.screen + "]") : "") +
                    (w.following ? ("  -> follows " + w.following) : "") +
                    ((w.age_sec || 0) > 30 ? ("  (stale " + seconds(w.age_sec) + ")") : ""));
            }
        }

        var authors = s.authors || [];
        if (authors.length) {
            lines.push("  action counts:");
            for (var p = 0; p < authors.length; p++) {
                var author = authors[p];
                lines.push("    " + (author.online ? "* " : "  ") + author.author +
                    "  actions " + (author.actions || 0) +
                    "  commits " + (author.commits || 0) +
                    "  last #" + (author.last_gseq || 0) +
                    " " + (author.last_type || "-"));
                lines.push("      " + typeSummary(author.by_type || {}));
            }
        }
        lines.push("");
    }
    setbody(lines.join("\n"));
}

function healthText(session) {
    if (session.journal_error) {
        return "JOURNAL ERROR: " + session.journal_error;
    }
    if (session.checkpoint_error) {
        return "CHECKPOINT ERROR: " + session.checkpoint_error;
    }
    return "ok";
}

function typeSummary(map) {
    var keys = Object.keys(map).sort(function (a, b) { return map[b] - map[a]; });
    if (!keys.length) {
        return "-";
    }
    var out = [];
    for (var i = 0; i < Math.min(8, keys.length); i++) {
        out.push(keys[i] + ":" + map[keys[i]]);
    }
    if (keys.length > 8) {
        out.push("...");
    }
    return out.join("  ");
}

function seconds(value) {
    value = Number(value) || 0;
    if (value < 60) {
        return Math.round(value) + "s";
    }
    if (value < 3600) {
        return Math.floor(value / 60) + "m" + Math.round(value % 60) + "s";
    }
    return Math.floor(value / 3600) + "h" + Math.floor((value % 3600) / 60) + "m";
}

function nowTime() {
    var d = new Date();
    function two(v) { return v < 10 ? "0" + v : String(v); }
    return two(d.getHours()) + ":" + two(d.getMinutes()) + ":" + two(d.getSeconds());
}

function setstatus(text) {
    outlet(0, "set", String(text));
}

function setbody(text) {
    outlet(1, "set", String(text));
    outlet(2, String(text));
}

function stringify(value) {
    try {
        return JSON.stringify(value, null, 2);
    } catch (e) {
        return String(value);
    }
}

function trim(value) {
    return String(value || "").replace(/^\s+|\s+$/g, "");
}
