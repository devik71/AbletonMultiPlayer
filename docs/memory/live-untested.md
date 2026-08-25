---
id: live-untested
type: state
title: Що написано, але жодного разу не працювало в живому Live
scope: all
status: current
updated: 2026-08-25
tags: [live, risk]
---

Станом на 2026-08-26 із реальним bridge не перевірялись: холодний архів і евікція сесій у relay, виправлення груп (b6c854c), Arrangement A і B для MIDI, ClipPropSet, ClipWarpSet, локатори, петля Arrangement, ReturnCreate/Delete, crossfade_assign, подвійна адресація кліпів. Через tools/pair-probe.mjs (живий Live як p1 плюс fake-live як p2) перевірено девʼять речей: присутність, follow із реальним рухом виду, обмін знімками, undo від імені партнера, diff без застосування, поіменна розбіжність можливостей, status, локи і СТИСНЕННЯ ХВОСТА -- пізній учасник дістав одну подію TempoSet замість шести. Передача файлів перевірена наскрізно. Це НЕ заміна прогону парою: емулятор не має справжніх LOM-listener-ів. Раніше на живому: SampleLoad, SongPropSet, SceneTimingSet, структура девайсів.

**Чому:** Це головний ризик проєкту і причина суфікса -dev у версії. Він легко забувається, бо тести зелені.
