@echo off
rem MES 코일 동기화 - stock_ledger_sync.py 와 같은 폴더에 두고 작업 스케줄러에 등록 (예: 매일 07:10, 19:40)
rem   schtasks /Create /TN "TJD_RawSync_AM" /TR "\"%~dp0mes_raw_sync.bat\"" /SC DAILY /ST 07:10 /RL HIGHEST /F
rem   schtasks /Create /TN "TJD_RawSync_PM" /TR "\"%~dp0mes_raw_sync.bat\"" /SC DAILY /ST 19:40 /RL HIGHEST /F
cd /d "%~dp0"
set PY=python
where py >nul 2>&1 && set PY=py -3
%PY% "%~dp0mes_raw_sync.py" --days 7
exit /b %ERRORLEVEL%
