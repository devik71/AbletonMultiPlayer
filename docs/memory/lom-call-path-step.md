---
id: lom-call-path-step
type: reference
title: "Крок шляху {'$call': ...} робить адресованим будь-який повернутий обʼєкт"
scope: all
status: current
updated: 2026-08-30
---

У /api/exec шлях може містити крок-виклик: {"$call": "метод", "args": [...]}. Приклад: ["tracks",0,"clip_slots",0,"clip",{"$call":"automation_envelope","args":[{"$path":["tracks",0,"devices",0,"parameters",1]}]}] адресує сам Envelope, у якого далі є insert_step і value_at_time. Правила доступу ті самі: приватні імена заборонені.

**Чому:** Половина цікавого в LOM живе не в атрибутах, а в тому, що метод ПОВЕРНУВ. Без цього кроку такий обʼєкт видно у відповіді й неможливо з ним нічого зробити -- саме через це конверт автоматизації здавався недосяжним навіть після того, як ми знайшли create_automation_envelope.
