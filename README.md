# Ableton Live Multiplayer — прототип

Шар синхронізації поверх Ableton Live. Візія і архітектура — [vision.md](vision.md),
протокол — [docs/PROTOCOL.md](docs/PROTOCOL.md).

**Статус: фаза 1.** Синхронізуються transport (play/stop), tempo і clip launch/stop
через сервер-секвенсор. Sample sync і audio monitor ще не початі.

## Складові

| Компонент | Де живе | Що робить |
|---|---|---|
| `remote-script/AbletonMP` | у процесі Live (Python) | LOM-listeners → UDP; застосування чужих подій до LOM |
| `daemon` | окремий процес на машині (Node) | UDP ⇄ WebSocket, реконект, буфер, clock sync |
| `relay` | один на сесію (Node) | присвоєння `global_seq`, hash-chain, журнал, broadcast |

## Запуск

### 1. Relay (одна машина, доступна обом)

```powershell
cd relay; npm install; npm start        # ws://0.0.0.0:19870
```

Стан: `http://<host>:19870/health`. Журнали — `relay/journals/<session>.jsonl`.

### 2. Daemon (на кожній машині)

```powershell
cd daemon; npm install
node index.js --author p1 --relay ws://127.0.0.1:19870 --session default
```

`--author` має бути різним у гравців: relay дедуплікує події по парі `(author, lseq)`.

### 3. Remote Script (на кожній машині)

```powershell
cd remote-script; .\install.ps1 -Symlink
```

Перезапустити Live → Preferences → Link/Tempo/MIDI → Control Surface → **AbletonMP**.
Лог bridge: `%APPDATA%\AbletonMP\bridge.log`.

## Друга машина

Relay слухає на всіх інтерфейсах, тож другій машині потрібна лише вихідна
зв'язність до нього — вхідних портів на ній не треба. `--author` має бути різним:
relay дедуплікує події по парі `(author, lseq)`.

**Проєкт треба копіювати як файл.** uuid лежать усередині `.als`, тож обидві машини,
відкривши копію того самого файлу, отримають однакову ідентичність ще до обміну.
Новий сет із такою ж структурою не підійде — там будуть інші uuid.

### Запуск daemon через SSH

Windows OpenSSH тримає сесію в Job-об'єкті з `KILL_ON_JOB_CLOSE`, тому процес,
запущений звичайним способом, помирає разом із SSH-сесією — `Start-Process` і
`-WindowStyle Hidden` від цього не рятують. Робочий варіант — створити процес
через WMI: він не стає нащадком викликача і job на нього не поширюється.

```powershell
Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = 'cmd.exe /c "C:\Users\<user>\run-daemon.cmd"'
}
```

де `run-daemon.cmd` робить `cd` у теку daemon і запускає `node index.js …`
з перенаправленням у лог (`schtasks` теж пробувався — завдання відпрацьовує
з кодом 0, але процес не лишається).

Логи daemon читати з явним кодуванням — `Get-Content -Encoding UTF8`, інакше
кирилиця побита.

## Перевірка без Live

`tools/fake-live.js` говорить тим самим UDP-протоколом, що й Remote Script.
Два фейкові клієнти + relay ганяють увесь ланцюг без DAW:

```powershell
# термінал 1
cd relay; npm start

# термінали 2-3 (daemon p1 і p2 на різних UDP-портах)
cd daemon; node index.js --author p1 --udp-in 19845 --udp-out 19846
cd daemon; node index.js --author p2 --udp-in 19847 --udp-out 19848

# термінали 4-5
cd daemon; node tools/fake-live.js --udp-in 19845 --udp-out 19846
cd daemon; node tools/fake-live.js --udp-in 19847 --udp-out 19848
```

У будь-якому fake-live: `play`, `tempo 128`, `launch 1 2`, `stopclip 1`, `state`.
Подія має зʼявитись у другого як `<- #N ...`.

## Що вже є і що ні

Треки і сцени адресуються стабільними uuid; реєстр народжується як подія
`RegistryInit` у журналі й переживає переставляння треків. Видалений об'єкт
працює як tombstone: подія на нього тихо ігнорується. uuid зберігаються
всередині `.als` через `set_data`, тож переживають і закриття проєкту —
за умови, що сет збережено.

Чого ще немає:

- **Переставляння треку не синхронізується.** Порядок у партнера залишиться свій.
  На адресацію не впливає (вона по uuid), але візуально проєкти розійдуться.
- **Реєстр не тестується e2e.** Персистентність спирається на `set_data` у Live,
  а fake-live його не має — перевіряти можна тільки на живому DAW.
- **Локальний Ctrl+Z у Live не породжує події** — LOM не дає хука на undo-стек.
  Відомий розсинхрон, див. vision.md §8.
- **Немає локів.** Одночасна зміна одного параметра = виграє останній за `global_seq`.
- **Snapshot не реконструює стан** — тільки діагностика в лозі daemon.
- **Правки Remote Script підхоплює лише повний рестарт Live.** Перезавантаження
  сету переінстанціює Control Surface, але модуль лишається в `sys.modules`.
