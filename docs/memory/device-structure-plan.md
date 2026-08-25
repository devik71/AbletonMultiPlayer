---
id: device-structure-plan
type: decision
title: Структуру девайсів робимо через LOM 12.3, переїзд окремим типом
scope: all
status: current
updated: 2026-08-25
tags: [devices, protocol]
---

LOM 12.3+ уміє те, заради чого ми дивились на Extensions: insert_device, delete_device, Song.move_device, а на раках -- insert_chain і Chain.insert_device/delete_device. Деталі в COVERAGE.md. Адреса девайса -- class_display_name, не class_name. Типи подій: DeviceInsert, DeviceDelete, DeviceMove; DeviceLoad лишається лише заради старих журналів. Переїзд окремим типом, обидва шляхи під ABLETONMP_DEVICE_MOVE (move за замовчуванням); у fake-live є команда movemode. Поріг версії нижчий, ніж здавалось: підпис Track.insert_device той самий і на 12.3.5. Стан на 2026-08-25: перевірено на живому Live з обох машин -- insert_device дав DeviceInsert, move_device через межу треку дав ОДИН DeviceMove (а не пару delete+insert), delete_device дав DeviceDelete. Ланцюги рака виміряні, але подіями ще не проганялись.

**Чому:** Виміряно на 12.3.8: пара delete+insert перестворює девайс із дефолтів (0.25 -> 0.899657) і на треку, і при виносі з ланцюга рака. move_device зберігає значення навіть через межу контейнера.
