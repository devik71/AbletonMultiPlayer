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
працює як tombstone: подія на нього тихо ігнорується.

Чого ще немає:

- **Переставляння треку не синхронізується.** Порядок у партнера залишиться свій.
  На адресацію не впливає (вона по uuid), але візуально проєкти розійдуться.
- **Персистентність реєстру між сесіями.** Наступного дня той самий проєкт
  отримає нові uuid: у Live немає куди покласти користувацькі дані на трек,
  тож потрібен sidecar-файл біля `.als`.
- **Локальний Ctrl+Z у Live не породжує події** — LOM не дає хука на undo-стек.
  Відомий розсинхрон, див. vision.md §8.
- **Немає локів.** Одночасна зміна одного параметра = виграє останній за `global_seq`.
- **Snapshot не реконструює стан** — тільки діагностика в лозі daemon.
- **Правки Remote Script підхоплює лише повний рестарт Live.** Перезавантаження
  сету переінстанціює Control Surface, але модуль лишається в `sys.modules`.
