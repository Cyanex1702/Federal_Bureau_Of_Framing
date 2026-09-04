@echo off
setlocal
title Federal Bureau of Framing V7.3.1

cd /d "%~dp0"

echo ============================================================
echo        FEDERAL BUREAU OF FRAMING V7.3.1
echo ============================================================
echo.
echo App folder:
echo %CD%
echo.

REM ------------------------------------------------------------
REM 1. Check Python
REM ------------------------------------------------------------
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python was not found.
    echo.
    echo Install Python and make sure "Add Python to PATH" is enabled.
    echo Then double-click RUN_APP.bat again.
    echo.
    pause
    exit /b 1
)

REM ------------------------------------------------------------
REM 2. Create virtual environment on first run
REM ------------------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo [SETUP] Creating virtual environment...
    python -m venv .venv

    if errorlevel 1 (
        echo.
        echo [ERROR] Could not create the virtual environment.
        pause
        exit /b 1
    )
)

set "PYTHON=.venv\Scripts\python.exe"

REM ------------------------------------------------------------
REM 3. Check required packages
REM ------------------------------------------------------------
echo [CHECK] Checking required Python packages...

"%PYTHON%" -c "import streamlit as st, PIL, numpy, cv2, requests, bs4, xlsxwriter; assert hasattr(st, 'fragment') and hasattr(st, 'iframe')" >nul 2>&1

if errorlevel 1 (
    echo [SETUP] Installing required packages...
    echo This can take a few minutes on the first run.
    echo.

    "%PYTHON%" -m pip install --upgrade pip
    "%PYTHON%" -m pip install -r requirements.txt

    if errorlevel 1 (
        echo.
        echo [ERROR] Package installation failed.
        echo Check your internet connection and try again.
        echo.
        pause
        exit /b 1
    )
)

REM ------------------------------------------------------------
REM 4. Start Streamlit
REM ------------------------------------------------------------
echo.
echo [START] Launching Federal Bureau of Framing...
echo.
echo Your browser should open automatically.
echo If it does not, open:
echo http://localhost:8501
echo.
echo Keep this window open while using the app.
echo Press Ctrl+C here to stop the app.
echo ============================================================
echo.

"%PYTHON%" -m streamlit run app.py

echo.
echo The app has stopped.
pause
