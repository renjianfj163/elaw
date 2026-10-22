@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>&1
if errorlevel 1 (
  echo 这台电脑还没有 Python。请先安装 Python 3.11 或更高版本，安装时勾选 Add python.exe to PATH。
  pause
  exit /b 1
)
if not exist "backend\.venv\Scripts\python.exe" (
  echo 第一次启动，正在准备运行环境……
  py -3 -m venv backend\.venv
  if errorlevel 1 goto fail
)
backend\.venv\Scripts\python -m pip install -r backend\requirements.txt
if errorlevel 1 goto fail
set PYTHONPATH=%CD%\backend
set ELAW_CONFIG_DIR=%CD%\config
set ELAW_GUARD=1
backend\.venv\Scripts\python -m app.local_launch
if errorlevel 1 goto fail
goto end
:fail
echo 启动没有成功。请把上面的报错留着。
pause
:end
