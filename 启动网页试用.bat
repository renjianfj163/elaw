@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONPATH=%CD%\backend"
set "ELAW_CONFIG_DIR=%CD%\config"
set "ELAW_PUBLIC=1"
set "ELAW_GUARD=0"
set "PYTHONNOUSERSITE=1"

if exist "%~dp0python\python.exe" (
  "%~dp0python\python.exe" -m app.public_serve
  if errorlevel 1 goto fail
  goto end
)

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
backend\.venv\Scripts\python -m app.public_serve
if errorlevel 1 goto fail
goto end
:fail
echo 在线试用没有启动。请确认已经设置 GEMINI_API_KEY，密钥不要写进网页或配置文件。
pause
:end
