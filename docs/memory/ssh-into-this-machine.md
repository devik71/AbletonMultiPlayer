---
id: ssh-into-this-machine
type: env
title: "SSH на робочу машину: ключ і пастка з адміністраторами"
scope: all
status: current
updated: 2026-08-30
verify: powershell -NoProfile -Command "(Get-Service sshd).Status"
expect: Running
---

На MISHA (192.168.3.24 Ethernet, 100.84.124.12 Tailscale) OpenSSH Server стоїть, sshd автозапуск, слухає 0.0.0.0:22, правило брандмауера 'OpenSSH Server (sshd)' з профілем Any -- це важливо, бо інтерфейс Ethernet у профілі Public, і друге правило (лише Private) саме по собі не спрацювало б. Акаунт MISHANYA локальний, БЕЗ пароля -- тому пароль по SSH не працює в принципі, лише ключ. Ключ для телефона: C:/Users/MISHANYA/.ssh/phone_ed25519. ПАСТКА: MISHANYA в групі Administrators, а для адміністраторів sshd читає НЕ ~/.ssh/authorized_keys, а C:/ProgramData/ssh/administrators_authorized_keys, і відмовляється читати цей файл, якщо доступ має хтось окрім SYSTEM і Administrators.

**Чому:** Ключ у звичному ~/.ssh/ у адміністратора мовчки не спрацьовує, і причину шукати довго. Плюс у файлі вже лежав чужий ключ claude-code@DESKTOP-B0MAT1T -- перезаписати його означало б відрізати іншій машині доступ.
