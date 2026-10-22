@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONPATH=%CD%\backend
set ELAW_CONFIG_DIR=%CD%\config
if exist "backend\.venv\Scripts\python.exe" (
  backend\.venv\Scripts\python -m app.local_launch --uninstall
  if errorlevel 1 goto fail
  goto end
)
reg delete HKCU\Software\Classes\elaw /f >nul 2>&1
echo 已尝试去掉协议注册。若桌面上还有「环评合规预审」快捷方式，可以手动删掉。
echo 保存在本机的数据还在 %LOCALAPPDATA%\elaw
goto end
:fail
echo 卸载没有完成。
pause
:end
