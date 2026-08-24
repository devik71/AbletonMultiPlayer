---
id: remote-script-install
type: env
title: Скрипт у Live живе окремою копією і сам не оновлюється
scope: all
status: current
updated: 2026-08-24
tags: [live, setup]
verify: node tools/check-install.mjs
expect: збіг
---

Live виконує НЕ файли репозиторію, а копію в `User Library/Remote Scripts/AbletonMP/`. Після кожної правки її треба перевстановити і перезапустити Live:

```
node tools/check-install.mjs            # звірити
node tools/check-install.mjs --install  # перезаписати
```

Шлях залежить від мови Windows і від того, чи перехопив теку OneDrive — інструмент шукає сам. Поруч лежить `openai_api_key`, його перезапис не чіпає.

**Чому:** 24 серпня зʼясувалось, що Live десять днів виконував копію від 14 серпня. Усі живі прогони за той час перевіряли код без Arrangement, груп і семплів — тобто не підтверджували нічого.
