---
id: param-renames-between-versions
type: reference
title: Ableton перейменовує параметри девайсів між мінорними версіями
scope: all
status: current
updated: 2026-08-27
tags: [lom, versions]
---

Виміряно на парі Live 12.3.5 (Windows) і 12.4.3 (macOS), той самий .als. Auto Filter: Morph -> Filter Morph, Type -> Filter Type, Slope -> Filter Slope. Reverb: In LowCut On -> In Lo Cut On, In HighCut On -> In Hi Cut On, In Filter Freq -> Input Freq. Compressor: Output Gain -> Output. Delay розійшовся на десять параметрів. Ми адресуємо параметр парою (name, ordinal), тож перейменований просто не знаходиться: подія доходить, приймальний бік пише 'parameter is absent, event skipped'. Від 27.08.2026 про це дізнається й АВТОР події через apply_gap.

**Чому:** Найдорожчий різновид розсинхрону: сети однакові, звучать по-різному, і в автора все виглядає робочим.
