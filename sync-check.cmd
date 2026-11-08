@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sync-check.ps1" %*
