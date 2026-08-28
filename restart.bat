@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   DpskLite-WebUI 一键重启
echo   (首次使用会自动安装依赖，之后快速跳过)
echo ============================================

set PY=python
where python >nul 2>nul || set PY=py

"%PY%" restart.py --install

echo.
echo 服务已退出。
pause
