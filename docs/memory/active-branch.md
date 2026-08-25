---
id: active-branch
type: state
title: Розробка йде в гілці agent/relay-hardening
scope: all
status: current
updated: 2026-08-25
tags: [git, workflow]
verify: git rev-parse --abbrev-ref HEAD
expect: agent/relay-hardening
---

Уся робота після 0.18 йде в agent/relay-hardening. У main лежить лише те, що пройшло перевірку; нетестоване туди не відправляємо свідомо. На 2026-08-26 гілка на 101 коміт попереду main, і main є її предком -- розходження немає. Знімати суфікс -dev і зливати в main -- після прогону парою. Перед тим: npm run preflight, далі node tools/pair-probe.mjs.

**Чому:** При переїзді між машинами перше, що губиться, -- у якій гілці працювати. Клон дає main, і робота починається не там.
