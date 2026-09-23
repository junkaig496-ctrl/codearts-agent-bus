@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo  agent-bus 一键演示（不依赖码道，100%% 可复现）
echo ============================================================
echo.
echo 正在重置总线并跑完整协作剧本...
python src\bus_cli.py reset --yes >nul 2>&1
python src\bus_cli.py demo
echo.
echo 正在打开协作看板（每 5 秒自动刷新）...
python src\bus_cli.py dashboard --open
echo.
echo 演示完成。看板文件在 %%USERPROFILE%%\.codeartsdoer\agent-bus\dashboard.html
pause
