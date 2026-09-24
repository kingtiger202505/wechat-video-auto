@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PIP_INDEX=-i https://pypi.tuna.tsinghua.edu.cn/simple

if not exist ".venv\Scripts\python.exe" (
  echo [安装] 创建虚拟环境 .venv ...
  py -3 -m venv .venv 2>nul || python -m venv .venv
  if not exist ".venv\Scripts\python.exe" (
    echo 创建虚拟环境失败：请先安装 Python 3.9~3.13 64位，并勾选 Add Python to PATH
    pause
    exit /b 1
  )
)
if not exist ".venv\installed.ok" (
  echo [安装] 安装依赖（首次运行需要几分钟）...
  ".venv\Scripts\python.exe" -m pip install -U pip %PIP_INDEX%
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt %PIP_INDEX%
  if errorlevel 1 (
    echo 依赖安装失败，请检查网络后重试
    pause
    exit /b 1
  )
  echo ok> ".venv\installed.ok"
)

".venv\Scripts\python.exe" -m wxva %*
echo.
pause
