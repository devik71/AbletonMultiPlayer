---
id: extensions-sdk-standard
type: decision
title: "Extensions SDK не використовуємо: ліцензія Standard"
scope: all
status: current
updated: 2026-08-24
tags: [ableton, scope]
---

Ableton Extensions SDK потребує Live 12 **Suite** 12.4.5+. На акаунті розробника Live Standard, і Suite-бету з нього не завантажити.

Перевірено на машині: бета 12.4.5b11 встановлена й авторизована, `ExtensionHostNodeModule.node` у неї є, але вкладки «Extensions» у налаштуваннях немає і Extension Host не стартує. Гейт — редакція, не збірка.

Probe-розширення лежить у `extension/`, заморожене, у Live не запускалось. Тарболи SDK і zip у `.gitignore` навмисно: ліцензія Ableton забороняє поширювати SDK поза власним застосунком.

Наслідок: структура девайсів, audio-кліпи, take lanes і рендер аудіо лишаються за Remote Script і LOM.

**Чому:** Спокуса повернутись до Extensions виникатиме щоразу, коли LOM чогось не вміє. Гейт ліцензійний і сам не зникне.
