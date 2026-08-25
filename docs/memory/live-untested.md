---
id: live-untested
type: state
title: Що написано, але жодного разу не працювало в живому Live
scope: all
status: current
updated: 2026-08-25
tags: [live, risk]
---

Станом на 2026-08-26 із реальним bridge не перевірялись: холодний архів і евікція сесій у relay, виправлення груп (b6c854c -- потребує ручного групування, LOM цього не вміє), і все, що додано після останнього перезапуску Live: ClipPropSet, ClipWarpSet, ChainMixerSet, локатори, петля Arrangement, ReturnCreate/Delete, crossfade_assign, подвійна адресація кліпів, власний розмір такту кліпа, ноти в лінійці, назви ланцюгів. Через tools/pair-probe.mjs перевірено десять речей із реальним bridge як p1: присутність, follow, обмін знімками, undo, diff, розбіжність можливостей, status, локи, стиснення хвоста і Arrangement для MIDI. Передача файлів наскрізна. Раніше на живому: SampleLoad, SongPropSet, SceneTimingSet, структура девайсів.

**Чому:** Це головний ризик проєкту і причина суфікса -dev у версії. Він легко забувається, бо тести зелені.
