---
id: live-script-stale
type: env
title: "Live тримає скрипт у пам'яті: файл на диску нічого не доводить"
scope: all
status: current
updated: 2026-08-24
verify: "node -e 'const s=require('fs').readFileSync('remote-script/AbletonMP/AbletonMP.py','utf8');process.stdout.write(s.includes('_script_sha')&&require('fs').readFileSync('daemon/index.js','utf8').includes('warnIfStaleScript')?'на місці':'зникло')'"
expect: на місці
---

SCRIPT_VERSION між комітами не змінюється, тож Live із учорашнім файлом у пам'яті виглядає точно як свіжий, а tools/check-install.mjs звіряє лише диск. Тепер bridge шле в hello sha -- перші 12 символів sha256 власного джерела, -- а daemon звіряє їх із файлом у репозиторії й каже вголос. Скрипт, старіший за саму цю здатність, теж ловиться: відсутність поля sha при наявності _script_sha у репозиторії і є доказом.

**Чому:** Це кусало тричі: партнера з 0.17, і двічі тут -- коли DeviceLoad мовчав і коли SceneTimingSet приходив як 'невідомий тип'. Щоразу шукали баг у логіці, а причина була в тому, що Live не перечитував файл.
