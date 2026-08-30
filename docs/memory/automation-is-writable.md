---
id: automation-is-writable
type: state
title: Автоматизація пишеться з Remote Script
scope: all
status: current
updated: 2026-08-30
---

Виміряно на живому 12.3.5 повним циклом: clip.automation_envelope(param) віддає None, поки конверта немає; clip.create_automation_envelope(param) створює його; Envelope має рівно два методи -- insert_step(time, length, value) і value_at_time(time); прибирати через clip.clear_all_envelopes(). Семантика часу: insert_step(1.0, 1.0, v) кладе значення на інтервал (1.0, 2.0] -- початок НЕ включений, кінець включений. Поза сходинками конверт віддає поточне значення параметра. Подій для цього ще не написано -- це наступний крок.

**Чому:** У COVERAGE.md і vision.md стояло протилежне, і на цій хибі трималась ціла гілка проєктування. Тепер там поправка, але сам синк автоматизації ще не реалізований -- легко забути, що межу знято, і далі обходити те, чого немає.
