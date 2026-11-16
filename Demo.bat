@echo off
rem Shows the dashboard with a fake camera roll (drawn shapes, not real photos).
title SnapSort Organizer (demo)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" -Demo
if errorlevel 1 pause
