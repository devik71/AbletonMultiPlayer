---
id: track-routing-sync
type: decision
title: "Маршрутизація: возимо документ, не залізо"
scope: all
status: current
updated: 2026-08-30
---

TrackRoutingSet синхронізує маршрут треку лише коли RoutingType.category у (3, 4, 6) -- Main, інший трек, No Input/No Output/Sends Only. Категорії 0 і 7 (Ext. Out, Ext. In, All Ins, Computer Keyboard) не анонсуються взагалі. Пишемо ТИПІЗОВАНО через available_*_routing_types, бо старий рядковий API суперечить сам собі: current_output_routing читає 'Master', а список пропонує 'Main', і запис 'Master' падає з 'The given IO target must be one of the available ones!'. Ціль шукається за uuid треку (attached_object), і лише як запасний варіант за назвою в тій самій категорії. Канали (Pre FX / Post FX / Track In) у v1 не переносяться.

**Чому:** У партнера інша звукова карта й інші входи: нав'язати йому свій Ext. In 3 означає зламати йому звук. Це та сама межа, що в personal-not-shared -- намір людини проти стану документа. А назва цілі ненадійна: '3-Audio' у партнера цілком може бути іншим треком, тож адреса -- uuid.
