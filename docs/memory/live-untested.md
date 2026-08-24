---
id: live-untested
type: state
title: Що написано, але жодного разу не працювало в живому Live
scope: all
status: current
updated: 2026-08-24
tags: [live, risk]
---

Станом на 2026-08-24 у живому Live не виконувалось: relay-hardening, знімки стану, обмін знімками, присутність і follow, undo, виправлення груп (b6c854c), Arrangement стадії A і B, SampleLoad для слоту Session. Усе це покрите юніт-тестами і наскрізним прогоном через fake-live, але емулятор не знає ні про синхронні LOM-listener-и, ні про те, як Live доправляє виділення. Структура девайсів із цього списку вийшла: DeviceInsert/DeviceMove/DeviceDelete перевірені на живому 12.3.8. Механізм SampleLoad (highlighted_clip_slot + load_item) виміряний на живому 12.3.5 вручну, але сама подія через bridge ще не проганялась.

**Чому:** Це головний ризик проєкту і причина суфікса -dev у версії. Він легко забувається, бо тести зелені.
