---
id: device-structure-plan
type: decision
title: Структуру девайсів робимо через LOM 12.3, переїзд окремим типом
scope: all
status: current
updated: 2026-08-24
tags: [devices, protocol]
---

LOM 12.3+ уміє те, заради чого ми дивились на Extensions: `insert_device`, `delete_device`, `Song.move_device`, а на раках — `insert_chain` і `Chain.insert_device`/`delete_device`. Усе виміряне на живому **12.3.8**, деталі в [COVERAGE.md](../COVERAGE.md).

Адреса девайса — `class_display_name`, не `class_name`.

Типи подій: `DeviceInsert`, `DeviceDelete`, `DeviceMove`. `DeviceLoad` лишається лише заради старих журналів.

Переїзд — **окремим типом**. Обидва шляхи під `ABLETONMP_DEVICE_MOVE` (`move` за замовчуванням); у `fake-live` є ще команда `movemode` для тестів.

Стан на 2026-08-24: bridge і `fake-live` зведені, набір тестів зелений (80 подій у наскрізному журналі), покриті вставка, видалення, переїзд між треками, звірка сигнатури і режим `pair`. Ланцюги рака виміряні на живому, але подіями ще не проганялись. **У живому Live новий шлях не працював жодного разу** — Live треба перезапустити, щоб він перечитав скрипт.

**Чому:** Виміряно на 12.3.8: пара delete+insert перестворює девайс із дефолтів (0.25 -> 0.899657) і на треку, і при виносі з ланцюга рака. move_device зберігає значення навіть через межу контейнера.
