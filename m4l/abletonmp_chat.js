autowatch = 1;
inlets = 1;
outlets = 3;

var baseUrl = "http://127.0.0.1:19847";
var authToken = "";
var promptText = "";
var executePlan = 1;
var busy = 0;

function loadbang() {
    authtoken();
    status();
}

function seturl() {
    baseUrl = arrayfromargs(arguments).join(" ").replace(/\/+$/, "");
    if (!baseUrl) {
        baseUrl = "http://127.0.0.1:19847";
    }
    setstatus("URL " + baseUrl);
}

function token() {
    authToken = trim(arrayfromargs(arguments).join(" "));
    setstatus(authToken ? "Token set" : "Token missing");
}

function authtoken() {
    var candidates = [
        "/Users/macbook/.abletonmp/chat_token",
        "/Users/macbook/Music/Ableton/User Library/Remote Scripts/AbletonMP/AbletonMP/chat_token"
    ];
    for (var i = 0; i < candidates.length; i++) {
        var value = readfile(candidates[i]);
        if (value) {
            authToken = value;
            setstatus("Token loaded");
            return;
        }
    }
    setstatus("Paste token, then Auth");
}

function setprompt() {
    promptText = arrayfromargs(arguments).join(" ");
}

function execute(v) {
    executePlan = Number(v) ? 1 : 0;
    setstatus(executePlan ? "Execute on" : "Execute off");
}

function ask() {
    var text = trim(promptText);
    if (!text) {
        setstatus("Prompt is empty");
        return;
    }
    request("POST", "/api/chat", { message: text, execute: executePlan ? true : false }, function (ok, data) {
        if (!ok) {
            setresponse("Chat failed:\n" + stringify(data));
            return;
        }
        var out = data.reply || "";
        if (data.needs_confirmation) {
            out += "\n\nNeeds confirmation.";
        }
        if (data.actions && data.actions.length) {
            out += "\n\nActions:\n" + stringify(data.actions);
        }
        if (data.executed) {
            out += "\n\nExecuted.";
        }
        if (data.result) {
            out += "\n\nResult:\n" + stringify(data.result);
        }
        setresponse(out || stringify(data));
    });
}

function snapshot() {
    request("GET", "/api/snapshot", null, function (ok, data) {
        setresponse(ok ? stringify(data.snapshot || data) : "Snapshot failed:\n" + stringify(data));
    });
}

function status() {
    request("GET", "/api/status", null, function (ok, data) {
        if (!ok) {
            setstatus("Offline / unauthorized");
            setresponse("Status failed:\n" + stringify(data));
            return;
        }
        var ai = data.ai || {};
        setstatus("Live OK  AI " + (ai.ready ? "ready" : "missing key"));
        setresponse(stringify(data));
    });
}

function stop() {
    request("POST", "/api/exec", { actions: [{ op: "stop_all_clips" }] }, function (ok, data) {
        setresponse(ok ? "Stopped all clips.\n\n" + stringify(data) : "Stop failed:\n" + stringify(data));
    });
}

function runjson() {
    var text = trim(promptText);
    if (!text) {
        setstatus("JSON prompt is empty");
        return;
    }
    try {
        var plan = JSON.parse(text);
        request("POST", "/api/exec", plan, function (ok, data) {
            setresponse(ok ? "Executed JSON.\n\n" + stringify(data) : "JSON exec failed:\n" + stringify(data));
        });
    } catch (e) {
        setresponse("Invalid JSON:\n" + e);
    }
}

function request(method, path, body, callback) {
    if (busy) {
        setstatus("Busy");
        return;
    }
    if (!authToken) {
        authtoken();
    }
    if (!authToken) {
        setresponse("No auth token. Paste contents of ~/.abletonmp/chat_token into Token.");
        return;
    }
    busy = 1;
    setstatus(method + " " + path);
    try {
        var xhr = new XMLHttpRequest();
        xhr.onreadystatechange = function () {
            if (xhr.readyState !== 4) {
                return;
            }
            busy = 0;
            var data = xhr.responseText || "";
            try {
                data = JSON.parse(data);
            } catch (e) {
                data = { ok: false, error: xhr.responseText || String(e) };
            }
            if (xhr.status >= 200 && xhr.status < 300 && data.ok !== false) {
                setstatus("OK");
                callback(true, data);
            } else {
                setstatus("HTTP " + xhr.status);
                callback(false, data);
            }
        };
        xhr.open(method, baseUrl + path, true);
        xhr.setRequestHeader("X-AbletonMP-Token", authToken);
        if (body !== null) {
            xhr.setRequestHeader("Content-Type", "application/json");
            xhr.send(JSON.stringify(body));
        } else {
            xhr.send();
        }
    } catch (e) {
        busy = 0;
        setstatus("Request error");
        callback(false, { ok: false, error: String(e) });
    }
}

function setstatus(text) {
    outlet(0, "set", String(text));
}

function setresponse(text) {
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

function readfile(path) {
    try {
        var f = new File(path, "read");
        if (!f || !f.isopen) {
            return "";
        }
        var text = "";
        try {
            text = f.readline(4096);
        } catch (e1) {
            try {
                text = f.readline();
            } catch (e2) {
                text = "";
            }
        }
        f.close();
        return trim(text);
    } catch (e) {
        return "";
    }
}

function trim(value) {
    return String(value || "").replace(/^\s+|\s+$/g, "");
}
