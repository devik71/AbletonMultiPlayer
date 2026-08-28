@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem  Гравець 2: relay у нього НЕ піднімається, він підключається до першого.
rem
rem    start-p2.bat                     адресу спитає і запамʼятає
rem    start-p2.bat 100.64.0.7          адреса relay
rem    start-p2.bat 100.64.0.7 myjam    адреса і назва сесії

set "ADDRFILE=%~dp0.relay-address"

set "RELAY=%~1"
if not "%RELAY%"=="" goto have_addr
if exist "%ADDRFILE%" set /p RELAY=<"%ADDRFILE%"
if not "%RELAY%"=="" goto have_addr
set /p RELAY=  Адреса relay (IP гравця 1):
:have_addr

if "%RELAY%"=="" (
  echo   Без адреси relay підключатись нікуди.
  pause
  exit /b 1
)
>"%ADDRFILE%" echo %RELAY%

set "SESSION=%~2"
if "%SESSION%"=="" set "SESSION=jam"

echo.
echo   AbletonMP -- гравець 2, сесія "%SESSION%", relay %RELAY%
echo   ------------------------------------------------------------

rem  Найдорожча помилка в парі -- одна машина оновилась, друга ні.
node tools\check-install.mjs
if errorlevel 1 (
  echo.
  echo   ^>^> Скрипт у Live не збігається з репозиторієм.
  echo   ^>^> Постав його і перезапусти Live:  node tools\check-install.mjs --install
  echo.
  pause
)

echo.
echo   Це вікно -- пульт: who, status, diff, watch, follow, undo, pull
echo.
cd /d "%~dp0daemon"
node index.js --author p2 --session "%SESSION%" --relay ws://%RELAY%:19870

echo.
echo   daemon завершився.
pause
