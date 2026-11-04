@echo off
rem Aggiornamento manuale della dashboard LoL: doppio clic su questo file.
rem Se la API Key e' scaduta te ne chiede una nuova, la prova e la salva nel file .env.
rem Alla fine apre la dashboard nel browser.
cd /d "%~dp0"
python lol_dashboard.py --open %*
echo.
pause
