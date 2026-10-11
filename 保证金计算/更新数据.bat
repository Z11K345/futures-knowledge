@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在更新保证金数据（抓取广发交易提示 + 交易所结算价）...
echo.
python "更新保证金数据.py" 2>nul
if errorlevel 1 py -3 "更新保证金数据.py" 2>nul
if errorlevel 1 "C:\Users\GFQH-GF-ZK\.workbuddy\binaries\python\envs\default\Scripts\python.exe" "更新保证金数据.py"
echo.
echo 完成后刷新 index.html 即可看到最新数据。
pause
