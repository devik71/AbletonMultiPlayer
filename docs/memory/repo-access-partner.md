---
id: repo-access-partner
type: env
title: "Репозиторій приватний: як партнер отримує код"
scope: all
status: current
updated: 2026-08-30
---

github.com/devik71/AbletonMultiPlayer приватний -- анонімно GitHub віддає 404, git просить логін. Партнеру потрібен або доступ (Settings -> Collaborators, далі gh auth login чи PAT з правом repo), або бандл: git bundle create <файл> --all, і в нього git clone <файл> / git pull <файл> main. Бандл ~860 КБ, несе всю історію й теги, node_modules у нього не потрапляють. origin після клону з бандла вказує на файл -- перепризначається через git remote set-url.

**Чому:** Партнер двічі не міг потягнути репо і возив теки руками, без .git -- тоді git pull не працює взагалі й діагноз неочевидний. Бандл несе історію, тож наступного разу він доганяється мержем, а не перезаписом.
