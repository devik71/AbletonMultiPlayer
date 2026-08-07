# Протокол (фаза 1)

Три шари, два хопи. Bridge живе в процесі Live, daemon — окремий процес на тій самій
машині, relay — один на сесію.

```
Live (LOM)  ⇄  Bridge (Remote Script, Python)
                    ⇅  JSON over UDP, localhost
               Daemon (Node)
                    ⇅  JSON over WebSocket
                Relay / Sequencer (Node)
```

## 1. Bridge ⇄ Daemon — JSON over UDP

Один JSON-об'єкт на датаграму, UTF-8, без роздільників. Тільки localhost.

| Напрям | Порт |
|---|---|
| daemon слухає (bridge → daemon) | `19845` |
| bridge слухає (daemon → bridge) | `19846` |

### Bridge → Daemon

```jsonc
{"m":"hello","live":"12.3.8","script":"0.1.0","pid":1234}
{"m":"bye"}
{"m":"heartbeat","t":1723000000.0}
{"m":"event","type":"TransportSet","payload":{"playing":true},"lseq":7}
{"m":"snapshot","state":{"tempo":128.0,"playing":false,"tracks":[...]}}
{"m":"log","level":"warn","text":"..."}
```

### Daemon → Bridge

```jsonc
{"m":"apply","type":"TransportSet","payload":{"playing":true},"gseq":42}
{"m":"snapshot_request"}
{"m":"ping","t":1723000000.0}
```

`apply` — команда застосувати чужу (вже впорядковану сервером) подію до LOM.
Bridge при цьому оновлює last-known-value **до** запису в LOM, щоб власний listener
не породив ехо-подію (див. §4).

## 2. Daemon ⇄ Relay — JSON over WebSocket

### Client → Relay

```jsonc
{"m":"join","session":"default","author":"p1","since":0,"proto":1}
{"m":"submit","event":{"type":"TempoSet","payload":{"bpm":128},"author":"p1","lseq":7,"ts":1723000000.0}}
{"m":"ping","t0":1723000000.0}
```

`since` — останній `gseq`, який клієнт уже має. Relay дошле хвіст.

### Relay → Client

```jsonc
{"m":"welcome","author":"p1","session":"default","head":{"gseq":41,"hash":"ab12..."},"peers":["p2"]}
{"m":"commit","event":{...,"gseq":42,"prev_hash":"ab12...","hash":"cd34..."}}
{"m":"peers","peers":["p1","p2"]}
{"m":"pong","t0":...,"t1":...,"t2":...}
{"m":"error","code":"proto_mismatch","text":"..."}
```

### Clock sync

Класична NTP-схема. `t0` — час відправки клієнтом, `t1` — прийому сервером,
`t2` — відправки сервером, `t3` — прийому клієнтом.

```
offset = ((t1 - t0) + (t2 - t3)) / 2
rtt    =  (t3 - t0) - (t2 - t1)
```

## 3. Журнал і hash-chain

Relay пише `journal.jsonl` — один закомічений івент на рядок. Кожен івент:

```jsonc
{
  "gseq": 42,              // монотонний, присвоює виключно relay
  "prev_hash": "ab12...",  // hash попереднього івента ("" для gseq=1)
  "hash": "cd34...",       // sha256(prev_hash + canonical_json(body))
  "type": "TempoSet",
  "payload": {"bpm": 128},
  "author": "p1",
  "lseq": 7,               // локальна послідовність автора, для дедуплікації
  "ts": 1723000000.0,      // wall-clock автора, довідково; НЕ впливає на порядок
  "srv_ts": 1723000000.2   // wall-clock relay
}
```

`canonical_json` — ключі відсортовані, без пробілів. Поля `hash` немає в тілі,
яке хешується.

Дедуплікація на relay: пара `(author, lseq)` вже бачена → івент ігнорується
(ідемпотентний ре-сабміт після реконнекту).

## 4. Типи подій (фаза 1)

| Type | Payload | Apply у LOM |
|---|---|---|
| `TransportSet` | `{playing: bool}` | `song.start_playing()` / `stop_playing()` |
| `TempoSet` | `{bpm: float}` | `song.tempo = bpm` |
| `ClipLaunch` | `{track: {idx, name}, scene: {idx, name?}}` | `clip_slots[j].fire()` |
| `ClipStop` | `{track: {idx, name}}` | `track.stop_all_clips()` |
| `SceneLaunch` | `{scene: {idx, name?}}` | `scenes[j].fire()` |
| `StopAllClips` | `{}` | `song.stop_all_clips()` |

`name` у посиланні на сцену необовʼязкове: у сцен Live за замовчуванням імені немає
(цифри в UI — це індекси). Немає імені — немає контрольної суми, і це видно з payload.

### Коалесинг: журнал несе дії, а не зміни LOM

LOM повідомляє про кожен крок значення, а не про жест. Без згортання один рух ручки
tempo дає ~20 подій, а запуск сцени — по одній події на кожен трек. Bridge згортає це
до відправки:

- **Неперервні параметри** (`tempo`, далі — mixer/device) дебаунсяться: подія йде
  через `DEBOUNCE_SEC` (200 мс) тиші після останньої зміни. Під час довгого
  безперервного жесту спрацьовує стеля `DEBOUNCE_MAX_HOLD` (1 с) — подія все одно
  йде, щоб довгий рух не пропав при розриві. Це той самий checkpoint, що в
  vision.md §5.5.
- **Зміни слотів** накопичуються до наступного тіку (`update_display`, ~100 мс).
  Якщо два і більше треків поїхали на той самий індекс сцени — це `SceneLaunch`,
  одна подія. Супутні зупинки треків без кліпу в цій сцені не відправляються:
  `scene.fire()` на приймальному боці відтворить їх сам. Якщо ж усе, що змінилось,
  зупинилось **і ніде більше нічого не грає** — це `StopAllClips`. Друга умова
  обовʼязкова: без неї дві зупинки поспіль в одному тіку виглядали б як глобальний
  стоп і заглушили б партнеру решту треків.

Затримка до ~100 мс на clip launch неважлива: Live і так квантизує запуск до межі такту.

### playing_slot_index нормалізується

У Live кілька відʼємних значень означають «не грає» (-1 — нічого, -2 — є fired slot).
Дзеркало зводить усі відʼємні до -1. Без цього перехід -1 → -2 читається як зміна
і породжує фантомний `ClipStop`, який у партнера застосується як `stop_all_clips()`
і заглушить йому треки, яких дія взагалі не стосувалась.

### Адресація об'єктів — тимчасова

Фаза 1 адресує треки і сцени **за індексом**, з іменем як контрольною сумою.
Якщо ім'я за індексом не збігається — подія не застосовується, пишеться warning.
Це навмисно дешеве рішення для прототипу: воно ловить розсинхрон, але не переживає
вставку треку посередині.

Стабільна ідентичність (UUID ⇄ LOM-об'єкт, tombstone) — фаза 2, це передумова
для replay і undo з vision.md §5.

### Echo suppression

Bridge тримає дзеркало останніх відомих значень (`playing`, `tempo`,
`playing_slot_index` по треках). Listener породжує подію **лише якщо значення
відрізняється від дзеркала**. Перед застосуванням чужої команди bridge записує
очікуване значення в дзеркало, тож listener, що спрацює після запису в LOM,
нічого не відправить.
