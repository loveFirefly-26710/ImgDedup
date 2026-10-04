@echo off
rem NOTE: this file MUST stay GBK (cp936) + CRLF.
rem Saving it as UTF-8 makes cmd mis-parse every line with Chinese text.
chcp 936 >nul
title ImgDedup
cd /d "%~dp0"

rem 依次找解释器：项目内 .venv -> 开发机常见位置 -> PATH 上的 pythonw
set "PYW="
if exist "%~dp0.venv\Scripts\pythonw.exe" set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if not defined PYW if exist "E:\Develop\python\pythonw.exe" set "PYW=E:\Develop\python\pythonw.exe"
if not defined PYW for %%I in (pythonw.exe) do set "PYW=%%~$PATH:I"

echo ============================================
echo   ImgDedup - 图片去重
echo ============================================
echo.
echo 正在启动，请稍候（首次启动约 1-2 秒）...
echo 启动后本窗口会自动关闭，软件界面会弹出来。
echo.

if not defined PYW goto :no_pythonw

start "" "%PYW%" -m imgdedup_ui
exit /b 0

:no_pythonw
echo [错误] 没找到 pythonw.exe。
echo.
echo 请先装好 Python 3.13 并让 pythonw 出现在 PATH 上，
echo 或者在本项目里建一个 .venv，也可以直接把解释器路径
echo 写进本脚本的 PYW 变量。
echo.
pause
exit /b 1
