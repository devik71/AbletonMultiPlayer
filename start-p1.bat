@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem  Гравець 1: у нього крутиться relay, і він же грає.
rem  Relay іде окремим вікном -- щоб обидва логи було видно одночасно.
rem
rem    start-p1.bat            сесія "jam"
rem    start-p1.bat myjam      своя назва сесії

set "SESSION=%~1"
if "%SESSION%"=="" set "SESSION=jam"

echo.
echo   AbletonMP -- гравець 1 (relay + гра), сесія "%SESSION%"
echo   ------------------------------------------------------------

rem  Найдорожча помилка в парі -- одна машина оновилась, друга ні.
rem  Перевіряємо ДО старту, поки це ще нічого не коштує.
node tools\check-install.mjs
if errorlevel 1 (
  echo.
  echo   ^>^> Скрипт у Live не збігається з репозиторієм.
  echo   ^>^> Постав його і перезапусти Live:  node tools\check-install.mjs --install
  echo.
  pause
)

echo.
echo   Твоя адреса для партнера -- одна з цих:
rem  Друкуємо ГОЛУ адресу: саме її чекає start-p2.bat першим аргументом.
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do @for /f "tokens=*" %%b in ("%%a") do @echo        %%b
echo.

start "AbletonMP relay" cmd /k "cd /d "%~dp0relay" && node server.js"

rem  Даємо relay підняти порт, інакше daemon стукає в зачинені двері
timeout /t 2 /nobreak >nul

echo   Це вікно -- пульт: who, status, diff, watch, follow, undo, pull
echo.
cd /d "%~dp0daemon"
node index.js --author p1 --session "%SESSION%" --relay ws://127.0.0.1:19870

echo.
echo   daemon завершився. Вікно relay закривається окремо.
pause
