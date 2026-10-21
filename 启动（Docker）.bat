@echo off
chcp 65001 >nul
cd /d "%~dp0"
docker compose version >nul 2>&1
if errorlevel 1 (
  echo 没有检测到 Docker。可以改用「启动（无需Docker）.bat」。
  pause
  exit /b 1
)
if not exist .env copy .env.example .env
docker compose up -d --build
if errorlevel 1 goto fail
start "" http://127.0.0.1:8000/
echo 工作台地址： http://127.0.0.1:8000/
echo 停止时在这个目录执行： docker compose down
pause
goto end
:fail
echo Docker 启动没有成功。
pause
:end
