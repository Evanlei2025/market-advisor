@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 未找到项目虚拟环境 .venv。
  echo 请先创建项目虚拟环境，运行：
  echo   py -3.12 -m venv .venv
  echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
  echo 如 py 找不到 Python 3.12，请查看 README.md 中本机现有解释器路径。
  echo.
  pause
  exit /b 2
)
".venv\Scripts\python.exe" local_report.py
set "RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %RESULT%
