---
id: js-mirrors-drift
type: decision
title: Дзеркала на JS розходяться з bridge тихо -- і тести цього не бачать
scope: all
status: current
updated: 2026-08-26
tags: [parity, tests]
---

У проєкті три пари, де та сама семантика написана двічі: _state_to_ops (Python) і daemon/tools/state-ops.js; _full_state і fullState() у fake-live; _op_gap і opGap. Тести ганяють JS-половину, а в живому Live виконується Python -- тож розбіжність виглядає як робоча функція місяцями. 2026-08-26 знайдено одразу три: знімок bridge не містив song, cues і chains взагалі, _state_to_ops відстав на шість типів, а _op_gap не знав про адреси ланцюга й кліпа в лінійці. Тримає їх тепер test/state-parity.mjs (типи подій, поля знімка, види прогалин) плюс перевірки в test/compact.mjs і test/locks.mjs проти дубльованого диспетчера.

**Чому:** Правило: додав гілку в одну половину пари -- додай у другу тим же комітом, інакше зелені тести брешуть.
