---
id: sample-not-parameters
type: decision
title: Маркери семплу в Simpler -- не параметри девайса
scope: all
status: current
updated: 2026-08-27
tags: [lom, devices]
---

У Simpler дві незалежні речі. Ручки S Start, S Length, S Loop On, S Loop Length, S Loop Fade -- звичайні DeviceParameter, їдуть DeviceParamSet-ом. Маркери на хвилі живуть в окремому обʼєкті device.sample і параметрами не є: виміряно на 12.3.5, після запису S Start = 0.12 маркер sample.start_marker лишився нулем. Властивості Sample і пишуться, і СПОСТЕРІГАЮТЬСЯ (listener спрацьовує) -- перевірено наживо на парі, маркер 50000 доїхав з Windows на macOS. Синхронізуються сім: start_marker, end_marker, gain, warping, warp_mode, slicing_beat_division, beats_granulation_resolution. Маркери задані в семплах, не в долях і не в 0..1, тож стеля -- sample.length.

**Чому:** Це шаблон для решти стокових девайсів: параметри вже покриті DeviceParamSet, працювати треба лише з обʼєктним станом повз parameters.
