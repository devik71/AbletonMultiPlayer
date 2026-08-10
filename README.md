# Ableton Live Multiplayer

Шар синхронізації поверх Ableton Live: двоє продюсерів працюють над одним проєктом
одночасно з різних машин. Не форк і не патч Live — тонкий зовнішній шар навколо
легальних точок розширення.

Візія і компроміси — [vision.md](vision.md), протокол — [docs/PROTOCOL.md](docs/PROTOCOL.md).

> Проєкт не афілійований з Ableton і не містить їхнього коду. Взаємодія з Live
> йде виключно через задокументовані точки розширення — Remote Script і
> Live Object Model.

**Статус.** Працює на парі машин у локальній мережі, перевірено на Live 12.3.8
і 12.3.5 одночасно. Синхронізуються транспорт, темп, кліпи, сцени, структура
треків і мікшер. Ноти в кліпах і параметри девайсів — ще ні, тож два `.als`
після спільної сесії поки не збігаються повністю.

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
| `daemon` | UDP ⇄ WebSocket, реконект, буферизація, clock sync, синхронізація файлів |
| `relay` | єдине джерело порядку: `global_seq`, hash-chain, журнал, broadcast |

Порядок подій визначає сервер-секвенсор, а не годинники машин. Стан проєкту —
результат послідовного застосування журналу, а не «живий» стан якогось клієнта.

## Що синхронізується

| Подія | Що несе |
|---|---|
| `TransportSet`, `TempoSet` | play/stop, темп (з дебаунсом) |
| `ClipLaunch`, `ClipStop`, `SceneLaunch`, `StopAllClips` | запуск і зупинка |
| `TrackCreate`, `TrackDelete`, `SceneCreate`, `SceneDelete` | структура |
| `MixerSet`, `TrackToggle` | гучність, панорама, send-и, mute/solo/arm |
| `RegistryInit` | ідентичність об'єктів на старті сесії |

Треки і сцени адресуються **стабільними uuid**, не індексами. uuid зберігаються
всередині `.als` (`set_data`), тож дві машини, що відкрили копію того самого
файлу, отримують однакову ідентичність ще до будь-якого обміну.

Семпли з теки проєкту синхронізуються автоматично, окремим шаром — relay їх
не журналює, лише пересилає.

## Запуск

Потрібні Node 18+ і Ableton Live 11/12.

### Relay — на одній машині, доступній обом

```powershell
cd relay; npm install; npm start        # ws://0.0.0.0:19870
```

Стан сесій: `http://<host>:19870/health`. Журнали: `relay/journals/<session>.jsonl`.

### Daemon — на кожній машині

```powershell
cd daemon; npm install
node index.js --author p1 --session myproject --relay ws://<relay-host>:19870
```

`--author` має бути різним у гравців — relay дедуплікує події по парі
`(author, lseq)`. `--session` прив'язана до конкретного проєкту: під однією
сесією має відкриватись один і той самий `.als`.

Ключі: `--project <шлях>` (якщо сет ще не збережено і теку не вивести
автоматично), `--state-dir`, `--udp-in` / `--udp-out`.

### Remote Script — на кожній машині

```powershell
cd remote-script; .\install.ps1          # або -Symlink для розробки
```

Перезапустити Live → Preferences → Link/Tempo/MIDI → Control Surface → **AbletonMP**.
Лог: `%APPDATA%\AbletonMP\bridge.log`.

Правки скрипта підхоплює **лише повний рестарт Live**: перезавантаження сету
переінстанціює Control Surface, але модуль лишається в `sys.modules`.

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
npm test                   # 5 hardening/recovery + 23 E2E-перевірок
node test/e2e.mjs          # лише E2E; E2E_VERBOSE=1 для повного виводу
```

Ручний прогін: підняти relay, потім по парі daemon + fake-live на різних портах.
Команди fake-live: `play`, `tempo 128`, `launch 1 2`, `scene 3`, `vol 1 0.5`,
`mute 0`, `addtrack`, `move 0 2`, `state`.

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

**Немає локів.** Одночасна зміна одного параметра = виграє останній за `global_seq`.

**Relay — single point of failure.** Журнал сесії існує лише на його машині;
разом із нею зникне й історія подій.

**Ідентичність сцен слабша за трекову.** `Track` уміє `set_data`, `Scene` — ні,
тож uuid сцен зберігаються мапою з позиціями і ламаються від переставляння
сцен між сесіями.

## Ліцензія

MIT — див. [LICENSE](LICENSE).
