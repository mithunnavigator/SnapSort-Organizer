@echo off
rem Double-click this file to open SnapSort Organizer. The first time it sets itself up.
title SnapSort Organizer
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 pause
