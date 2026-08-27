---
id: live-restart-danger
type: env
title: Live не можна вбивати примусово
scope: all
status: current
updated: 2026-08-27
tags: [live, ops]
---

Stop-Process -Force і killall Live лишають Live у стані, коли наступний старт упирається в модальне «Live unexpectedly quit... recover your work?» і не піднімається, доки хтось не клацне. На віддаленій машині це зупиняє прогін. Стан відновлення лежить у Preferences/Crash, CrashDetection.cfg і CrashRecoveryInfo.cfg -- прибравши їх, Live стартує чисто. Друге: uuid обʼєктів потрапляють у .als лише при збереженні, тож убитий Live губить ідентичність, і наступна сесія прив'язує її за позицією.

**Чому:** Витрачено пів години прогону на розблокування Live, який мовчки стояв на діалозі. Save у LOM немає, автоматизувати збереження нічим.
