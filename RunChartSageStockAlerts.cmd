@echo off
setlocal
cd /d "%~dp0"
set "PYTHONNOUSERSITE=1"
set "PYTHONPATH="
python -s "%~dp0launch_stock_alerts.py" %*
if errorlevel 1 (
  echo.
  echo ChartSage Stock Alerts stopped with an error.
  pause
)
endlocal
