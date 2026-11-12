@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 鲜蔬源 · 蔬菜店铺销售管理系统

echo ============================================================
echo   鲜蔬源 · 蔬菜店铺销售管理系统
echo   正在启动本地服务，浏览器会自动打开...
echo   数据保存在 data\fengshi.db
echo ============================================================
echo.

set "PYTHON="
where py >nul 2>nul && set "PYTHON=py -3"
if not defined PYTHON (
    where python >nul 2>nul && set "PYTHON=python"
)
if not defined PYTHON (
    echo [错误] 没有检测到 Python 3。
    echo 请先安装 Python 3，安装时勾选 Add Python to PATH：
    echo     https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

%PYTHON% server.py

echo.
echo 系统已退出，数据已保存在 data\fengshi.db 中。
pause