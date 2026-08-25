---
id: live-script-stale
type: env
title: "Live тримає скрипт у пам'яті: файл на диску нічого не доводить"
scope: all
status: current
updated: 2026-08-25
verify: git grep -l warnIfStaleScript -- daemon/index.js
expect: daemon/index.js
---

SCRIPT_VERSION між комітами не змінюється, тож Live з учорашнім файлом у пам'яті виглядає точно як свіжий, а tools/check-install.mjs звіряє лише диск. Тепер bridge шле в hello sha -- перші 12 символів sha256 власного джерела, -- а daemon звіряє їх із файлом у репозиторії й каже вголос. Скрипт, СТАРІШИЙ за саму цю здатність, теж ловиться: відсутність поля sha при наявності _script_sha у репозиторії і є доказом.

**Чому:** Це кусало тричі: партнера з 0.17, і двічі тут -- коли DeviceLoad мовчав і коли SceneTimingSet приходив як 'невідомий тип'. Щоразу шукали баг у логіці, а причина була в тому, що Live не перечитував файл.
