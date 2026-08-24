---
id: active-branch
type: state
title: Розробка йде в гілці agent/relay-hardening
scope: all
status: current
updated: 2026-08-24
tags: [git, workflow]
verify: git branch --show-current
expect: agent/relay-hardening
---

Уся робота після 0.18 йде в `agent/relay-hardening`. У `main` лежить лише те, що пройшло перевірку; нетестоване туди не відправляємо свідомо.

На 2026-08-24 гілка на 41 коміт попереду `main`, і `main` є її предком — розходження немає.

**Чому:** При переїзді між машинами перше, що губиться, — у якій гілці працювати. Клон дає `main`, і робота починається не там.
