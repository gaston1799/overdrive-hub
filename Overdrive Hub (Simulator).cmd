@echo off
rem Simulator: 4 virtual cars, separate data, Bluetooth off
start "" "%~dp0app\node_modules\electron\dist\electron.exe" "%~dp0app" --sim 4 --no-ble
