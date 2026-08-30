---
id: snapshot-carries-envelopes-routing
type: state
title: Знімок несе конверти й маршрути -- перевірено на живому
scope: all
status: current
updated: 2026-08-30
---

У _full_state (той, що їде по дроту) трек має ключ routing, кліп -- envelopes. Перевірено на живому 12.3.5: routing = {'output': {'category': 3, 'name': 'Main'}}, envelopes = [{device: Auto Filter, parameter: Frequency, steps: [[0, 0.25], [2, 0.8]]}]. Вхід у знімок не потрапив і не мусив -- він Ext: All Ins, тобто залізо. УВАГА: /api/exec op snapshot віддає ІНШИЙ, діагностичний _ai_snapshot, у якому цих полів немає; перевіряти треба daemon/state/<author>.<session>.state.json після команди state.

**Чому:** Я двічі шукав нові поля не в тому знімку й вирішив, що вони не додались. Два різні знімки з однаковою назвою -- готова пастка для наступного разу.
