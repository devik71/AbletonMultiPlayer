# Ableton Live Multiplayer

Шар синхронізації поверх Ableton Live: двоє продюсерів працюють над одним проєктом
одночасно з різних машин. Не форк і не патч Live — тонкий зовнішній шар навколо
легальних точок розширення.

Візія і компроміси — [vision.md](vision.md), протокол — [docs/PROTOCOL.md](docs/PROTOCOL.md).

> Проєкт не афілійований з Ableton і не містить їхнього коду. Взаємодія з Live
> йде виключно через задокументовані точки розширення — Remote Script і
> Live Object Model.

**Поточна версія: 0.18.0.** Працює на парі машин у локальній мережі, перевірено
на Live 12.3.8 і 12.3.5 одночасно. Синхронізуються транспорт, темп, запуск кліпів
і сцен, структура треків, мікшер, а від версії bridge 0.12 —
створення/видалення Session MIDI-кліпів та їхні ноти. Від bridge 0.13
синхронізуються параметри верхньорівневих девайсів на звичайних треках, від 0.14 —
параметри девайсів усередині вкладених Rack chains, від 0.15 — девайси на Return
Tracks і Master Track, від 0.16 — мікшер Return/Master, а від 0.17 — назви й
кольори Track/Scene/Session Clip. Від 0.18 додано authenticated AI Chat/LOM API,
Max for Live chat device і Max for Live relay status monitor. Структура девайсів
і Arrangement — ще ні, тож два `.als` після спільної сесії поки не збігаються
повністю.

## Що нового в 0.18.0

- Authenticated localhost AI Chat API у Remote Script: `http://127.0.0.1:19847/`.
- OpenAI planner для правок через Live Object Model з чергою виконання на Live thread.
- Editable Max patch `AbletonMP AI Chat.maxpat` для prompt/token/execute/snapshot прямо в Live.
- Розширений relay `GET /health`: кімнати, онлайн-гравці, IP, версії Live/script,
  кількість дій по author і breakdown за типами подій.
- Editable Max patch `AbletonMP Multiplayer Status.maxpat` як чисте info/log вікно
  для статусу relay і кімнати.
- Версії npm-пакетів, bridge і fake-live вирівняні на `0.18.0`.

## Як це влаштовано

```
Live (LOM)  ⇄  Bridge (Remote Script, Python, у процесі Live)
                    ⇅  JSON over UDP, localhost
               Daemon (Node, по одному на машину)
                    ⇅  JSON over WebSocket
                Relay / Sequencer (Node, один на сесію)
```

| Компонент | Відповідальність |
|---|---|
| `remote-script/AbletonMP` | LOM-listeners → події; застосування чужих подій до LOM. Максимально тонкий: виняток тут може завалити Live |
| `daemon` | UDP ⇄ WebSocket, реконект, буферизація, clock sync, синхронізація файлів, межі жесту для локів |
| `relay` | єдине джерело порядку: `global_seq`, hash-chain, журнал, broadcast, стиснення хвоста на join, арбітраж локів |

Порядок подій визначає сервер-секвенсор, а не годинники машин. Стан проєкту —
результат послідовного застосування журналу, а не «живий» стан якогось клієнта.

## Що синхронізується

| Подія | Що несе |
|---|---|
| `TransportSet`, `TempoSet` | play/stop, темп (з дебаунсом) |
| `ClipLaunch`, `ClipStop`, `SceneLaunch`, `StopAllClips` | запуск і зупинка |
| `TrackCreate`, `TrackDelete`, `SceneCreate`, `SceneDelete` | структура |
| `MixerSet`, `TrackToggle` | мікшер звичайних/Return/Master треків (включно crossfader і cue) |
| `ObjectMetaSet` | назва і RGB-колір Track/Return/Master, Scene та Session Clip |
| `DeviceParamSet` | automatable-параметри девайсів звичайних/Return/Master треків, включно із вкладеними Rack chains |
| `ClipCreate`, `ClipDelete`, `ClipNotesSet` | Session MIDI-кліпи та ноти |
| `RegistryInit` | ідентичність об'єктів на старті сесії |

Треки і сцени адресуються **стабільними uuid**, не індексами. uuid зберігаються
всередині `.als` (`set_data`), тож дві машини, що відкрили копію того самого
файлу, отримують однакову ідентичність ще до будь-якого обміну.

Return Tracks і Master Track мають окремі aux-uuid. У `DeviceParamSet` вони явно
позначаються як `track.kind: "return"|"master"`, тому їх неможливо помилково
застосувати до звичайного треку з таким самим id.

Семпли з теки проєкту синхронізуються автоматично, окремим шаром — relay їх
не журналює, лише пересилає.

## Встановлення і запуск

Потрібні:

- Node.js 18+.
- Ableton Live 11/12.
- Max for Live, якщо потрібні вбудовані UI-патчі.
- OpenAI API key лише для AI Chat; базовий multiplayer працює без нього.

### 1. Завантажити репозиторій

```bash
git clone https://github.com/devik71/AbletonMultiPlayer.git
cd AbletonMultiPlayer
```

Якщо ти працюєш із локальною папкою без git, достатньо перейти в її корінь:

```bash
cd /Users/macbook/Desktop/AbletonMultiPlayer-main
```

### 2. Relay — одна машина на сесію

Relay має бути доступним усім гравцям у локальній мережі:

```bash
cd relay
npm install
npm start
```

За замовчуванням він слухає `ws://0.0.0.0:19870`. Стан сесій:
`http://<relay-host>:19870/health`. Endpoint показує кімнати, head журналу,
онлайн-гравців, їхні IP, Live/script версії, кількість дій по авторах і breakdown
за типами подій. Журнали лежать у `relay/journals/<session>.jsonl`.

Мовчазні зʼєднання relay пінгує кожні 15 с (`MP_HEARTBEAT_SEC`) і розриває
після 45 с тиші (`MP_STALE_SEC`), тож гравець, у якого зник Wi-Fi, зникає
з `peers` одразу, а не за системним таймаутом TCP. На `Ctrl+C` relay прощається
з клієнтами штатно — вони йдуть у звичайний реконект.

Relay за замовчуванням відкритий: хто дістав до порту, той у сесії. Для спільної
мережі або тунелю задай токен — і той самий рядок передай кожному daemon:

```bash
MP_RELAY_TOKEN=довгий-випадковий-рядок npm start
```

```bash
node index.js --author p1 --session Untitled --relay ws://127.0.0.1:19870 --token довгий-випадковий-рядок
```

Темп подій обмежений: `MP_SUBMIT_RATE` подій/с (100) зі сплеском `MP_SUBMIT_BURST`
(300) на з'єднання. Це страховка від клієнта, що зациклився; відхилена подія не
губиться — daemon тримає її в буфері й повторює через секунду.

Хвіст журналу, який relay віддає гравцю на join, стискається: із серії подій на
одну адресу (те саме положення фейдера, той самий параметр девайса, той самий
регіон нот) доїжджає остання. Журнал на диску лишається повним, а `MP_COMPACT_JOIN=0`
вимикає стиснення. Скільки подій зекономлено, видно в `/health`
(`served_events`, `dropped_events`).

### 3. Daemon — на кожній машині

Відкрити другий термінал:

```bash
cd daemon
npm install
node index.js --author p1 --session Untitled --relay ws://127.0.0.1:19870
```

Для другої машини `--relay` має вказувати IP машини з relay, наприклад
`ws://192.168.3.18:19870`. `--author` має бути різним у гравців: `p1`, `p2`,
`devik`, `laptop` тощо. Relay дедуплікує події по парі `(author, lseq)`.

Якщо relay захищений токеном, додай `--token` з тим самим рядком.

`--session` прив'язана до конкретного проєкту: під однією сесією має відкриватись
один і той самий `.als`. Корисні ключі: `--project <шлях>` (якщо сет ще не
збережено і теку не вивести автоматично), `--state-dir`, `--udp-in`, `--udp-out`.

### 4. Remote Script — на кожній машині з Live

#### macOS

Для розробки зручно поставити symlink, щоб Live бачив зміни з репозиторію після
повного рестарту:

```bash
REMOTE="$HOME/Music/Ableton/User Library/Remote Scripts"
mkdir -p "$REMOTE"
ln -s "$(pwd)/remote-script/AbletonMP" "$REMOTE/AbletonMP"
```

Якщо `"$REMOTE/AbletonMP"` уже існує як стара копія, спочатку прибери або перейменуй
її, а потім створи symlink. Альтернатива без symlink:

```bash
rsync -a --delete "$(pwd)/remote-script/AbletonMP/" \
  "$HOME/Music/Ableton/User Library/Remote Scripts/AbletonMP/"
```

#### Windows

```powershell
cd remote-script
.\install.ps1          # копія
.\install.ps1 -Symlink # symlink для розробки
```

Після встановлення повністю перезапустити Live, потім вибрати:
Preferences -> Link/Tempo/MIDI -> Control Surface -> **AbletonMP**.

Лог bridge:

- macOS: `$TMPDIR/AbletonMP/bridge.log`.
- Windows: `%APPDATA%\AbletonMP\bridge.log`.

Правки Remote Script підхоплює **лише повний рестарт Live**. Перезавантаження сету
переінстанціює Control Surface, але модуль лишається в `sys.modules`.

### 5. OpenAI key для AI Chat

Ключ потрібен тільки для natural-language правок у Live. Поклади його одним рядком
в один із цих файлів:

```bash
mkdir -p "$HOME/.abletonmp"
printf '%s\n' 'sk-...' > "$HOME/.abletonmp/openai_api_key"
```

Або локально біля Remote Script:

```bash
printf '%s\n' 'sk-...' > remote-script/AbletonMP/openai_api_key
```

Ці шляхи вже закриті `.gitignore`. Token для локального chat API створюється
автоматично в `~/.abletonmp/chat_token`; M4L UI зазвичай читає його сам.

### 6. Max for Live AI Chat UI

Remote Script піднімає authenticated localhost UI/API для AI-правок:
`http://127.0.0.1:19847/`.

Editable patch:

```text
m4l/AbletonMP AI Chat.maxpat
m4l/abletonmp_chat.js
```

Відкрити `.maxpat` у Max, перевірити що `abletonmp_chat.js` лежить поруч, потім
`File -> Save As...` як Max for Live device (`.amxd`) у User Library. Patch має
prompt, token, execute toggle, `ask`, `snapshot`, `stop` і `runjson`. Якщо token
не підтягнувся автоматично, встав його вручну в поле Token.

### 7. Max for Live Multiplayer Status

Окремий info/log device для multiplayer relay:

```text
m4l/AbletonMP Multiplayer Status.maxpat
m4l/abletonmp_multiplayer_status.js
```

Він опитує `http://127.0.0.1:19870/health` кожні ~2 секунди і показує статус
сервера, кімнати, онлайн-гравців, IP, версії Live/script, скільки дій зробив
кожен author, останню подію, розподіл подій за типами і хто що редагує просто зараз. Для віддаленого relay
в полі Relay вказати `http://<relay-ip>:19870`; поле Room можна лишити пустим,
або вписати конкретну `--session`.

## Друга машина

Проєкт треба скопіювати **файлом**: uuid лежать усередині `.als`, тож новий сет
із такою ж структурою не підійде. Relay слухає на всіх інтерфейсах, і другій
машині потрібна лише вихідна зв'язність — вхідних портів на ній не треба.

Запуск daemon через SSH має пастку: Windows OpenSSH тримає сесію в Job-об'єкті
з `KILL_ON_JOB_CLOSE`, тож процес помирає разом із SSH. Ні `Start-Process`,
ні `schtasks` не рятують. Робочий варіант — створити процес через WMI, бо він
не стає нащадком викликача:

```powershell
Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = 'cmd.exe /c "C:\Users\<user>\run-daemon.cmd"'
}
```

Логи daemon читати з `Get-Content -Encoding UTF8`, інакше кирилиця побита.

## Перевірка без Live

`daemon/tools/fake-live.js` говорить тим самим UDP-протоколом, що й Remote Script.
Весь ланцюг ганяється без DAW:

```powershell
npm test                   # 25 unit/hardening + 44 E2E-перевірки
node test/e2e.mjs          # лише E2E; E2E_VERBOSE=1 для повного виводу
```

Ручний прогін: підняти relay, потім по парі daemon + fake-live на різних портах.
Команди fake-live: `play`, `tempo 128`, `launch 1 2`, `scene 3`, `vol 1 0.5`,
`mute 0`, `addtrack`, `move 0 2`, `note 0 0 60 0 1 100`, `delnote 0 0 60 0`,
`delclip 0 0`, `device 0 0 1 0.75`, `device 0 3/1/0/0/0 0 0.9`,
`device return:0 0 0 0.5`, `device master 0 0 0.5`, `mix return:0 volume 0.5`,
`toggle return:0 mute`, `mix master crossfader 0.5`, `meta scene:0 name Intro`,
`meta clip:0:0 color 16755200`, `state`.
У `device` шлях через `/` чергує індекси device/chain/device; останній елемент —
цільовий device, після шляху йдуть індекс параметра та значення.

`test/inject.mjs` кидає одну подію в relay від імені окремого учасника — зручно
перевіряти застосування в справжньому Live без другої машини.
У PowerShell JSON надійніше передавати через `$env:MP_PAYLOAD='{"bpm":135}'`,
а потім запускати `node test/inject.mjs TempoSet` — native argument parsing може
зняти внутрішні лапки з inline payload.

## Межі, про які варто знати

**Семпли поза текою проєкту не працюють.** Live за замовчуванням не копіює семпл
у проєкт — `.als` тримає посилання на оригінал. У партнера цього шляху немає.
Виправити це кодом не можна: `Collect All and Save` через LOM не викликається,
а `clip.file_path` доступний лише на читання. Daemon повідомляє, скільки семплів
поза проєктом, але зібрати їх має користувач.

**Видалення файлів не поширюються.** Маніфест каже лише «що я маю», тож зниклий
семпл партнер віддасть назад. Прибирати треба на обох машинах.

**Локальний Ctrl+Z у Live не породжує події** — LOM не дає хука на undo-стек.
Відомий розсинхрон.

**MIDI-синхронізація поки охоплює лише Session View.** Arrangement-кліпи,
audio clip creation, clip envelopes, loop/start markers і довжина вже
наявного кліпу не спостерігаються. Для екстремально щільного MIDI один нотний
тайл може перевищити ліміт UDP-датаграми; bridge запише `datagram too large` у лог.

**Локи — порада, а не заборона.** Коли гравець крутить фейдер, параметр девайса
або малює ноти, daemon бере лок на цей обʼєкт, і партнер бачить «p1 редагує Bass»
у своєму лозі та в M4L Status device. Але relay нікого не блокує: одночасна зміна
одного параметра і далі вирішується за `global_seq` — виграє останній. Заборона
вимагала б відкату вже застосованої локально зміни, а replay з snapshot ще немає.

**Структура девайсів не синхронізується.** Звичайні, Return і Master треки мають
містити однакові Rack/Chain/device дерева на обох машинах. Невідповідний locator
дає warning/no-op, а не застосування до схожого девайса в іншому контейнері.

**Relay — single point of failure.** Журнал сесії існує лише на його машині;
разом із нею зникне й історія подій.

**Ідентичність сцен слабша за трекову.** `Track` уміє `set_data`, `Scene` — ні,
тож uuid сцен зберігаються мапою з позиціями і ламаються від переставляння
сцен між сесіями.

## Ліцензія

MIT — див. [LICENSE](LICENSE).
