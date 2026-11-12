@echo off
title DeepNoise - Record 30s Bluetooth Audio Sample
cd /d "%~dp0.."
python "scripts\record_bluetooth_sample.py"
pause

