@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo  把 agent-bus 技能装到「个人级」（本机所有项目可用）
echo ============================================================
set DEST=%USERPROFILE%\.codeartsdoer\skills\agent-bus
if not exist "%USERPROFILE%\.codeartsdoer\skills" mkdir "%USERPROFILE%\.codeartsdoer\skills"
xcopy /E /I /Y "skills\agent-bus" "%DEST%" >nul
if errorlevel 1 (
  echo 拷贝失败，请手动把 skills\agent-bus 复制到 %DEST%
) else (
  echo 已安装到：%DEST%
  echo.
  echo 下一步：在码道里打开 设置 - 技能与规则 - 个人级，确认 agent-bus 状态为「已开启」。
)
echo.
echo 提示：只想给当前项目用的话，把 skills\agent-bus 拷到
echo       ^<项目根目录^>\.codeartsdoer\skills\ 即可。
pause
