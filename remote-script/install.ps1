<#
.SYNOPSIS
  Ставить AbletonMP у User Library Ableton Live.

.DESCRIPTION
  За замовчуванням копіює папку скрипта. Під час розробки зручніше -Symlink:
  тоді правки в репо підхоплюються після рестарту Live без переінсталяції
  (потребує прав адміністратора або увімкненого Developer Mode).

  Після інсталяції: Live > Preferences > Link/Tempo/MIDI > Control Surface > AbletonMP.
  Live читає скрипти тільки при старті — перезапусти його.

.EXAMPLE
  .\install.ps1
  .\install.ps1 -Symlink
  .\install.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Symlink,
    [switch]$Uninstall,
    [string]$UserLibrary
)

$ErrorActionPreference = 'Stop'
$src = Join-Path $PSScriptRoot 'AbletonMP'

if (-not $UserLibrary) {
    $UserLibrary = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Ableton\User Library'
}
if (-not (Test-Path $UserLibrary)) {
    Write-Error "User Library не знайдено: $UserLibrary`nВкажи вручну: -UserLibrary '<шлях>'"
}

$scriptsDir = Join-Path $UserLibrary 'Remote Scripts'
$dest = Join-Path $scriptsDir 'AbletonMP'

if ($Uninstall) {
    if (Test-Path $dest) {
        # для симлінка Remove-Item -Recurse пішов би вглиб і повидаляв вихідники
        if ((Get-Item $dest).LinkType) { (Get-Item $dest).Delete() }
        else { Remove-Item $dest -Recurse -Force }
        Write-Host "Видалено: $dest" -ForegroundColor Yellow
    } else {
        Write-Host "Не встановлено — нічого робити." -ForegroundColor Yellow
    }
    return
}

if (-not (Test-Path $scriptsDir)) {
    New-Item -ItemType Directory -Path $scriptsDir -Force | Out-Null
    Write-Host "Створено $scriptsDir"
}

if (Test-Path $dest) {
    if ((Get-Item $dest).LinkType) { (Get-Item $dest).Delete() }
    else { Remove-Item $dest -Recurse -Force }
}

if ($Symlink) {
    try {
        New-Item -ItemType SymbolicLink -Path $dest -Target $src | Out-Null
        Write-Host "Симлінк: $dest -> $src" -ForegroundColor Green
    } catch {
        Write-Error "Симлінк не вдався (потрібен адмін або Developer Mode). Запусти без -Symlink."
    }
} else {
    Copy-Item $src $dest -Recurse -Force
    Write-Host "Скопійовано: $dest" -ForegroundColor Green
}

# Кеш .pyc від попередньої версії Live підхоплює старий байт-код
Get-ChildItem $dest -Recurse -Include '__pycache__', '*.pyc' -Force -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Далі:" -ForegroundColor Cyan
Write-Host "  1. Перезапусти Ableton Live (скрипти читаються лише при старті)."
Write-Host "  2. Preferences > Link/Tempo/MIDI > Control Surface > AbletonMP."
Write-Host "  3. Лог: $env:APPDATA\AbletonMP\bridge.log"
