@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo  agent-bus 全量自检
echo ============================================================
echo.
echo [1/2] 组件级测试（核心链路 / 跨进程并发 / MCP 协议握手）
python tests\test_e2e.py
if errorlevel 1 goto fail
echo.
echo [2/2] 双会话集成测试（两个独立 MCP 进程 = 码道的两个会话）
python tests\test_two_sessions.py
if errorlevel 1 goto fail
echo.
echo 全部通过。可以接进码道了。
pause
exit /b 0

:fail
echo.
echo 有测试未通过：请把上面的失败项截图反馈。
pause
exit /b 1
