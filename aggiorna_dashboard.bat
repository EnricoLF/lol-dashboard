@echo off
rem Aggiorna subito la dashboard online: avvia il workflow "Aggiorna dashboard" su GitHub,
rem aspetta che finisca e apre la pagina. Doppio clic su questo file.
rem Serve GitHub CLI (gh) collegato all'account EnricoLF; senza, apre solo la pagina,
rem che comunque si aggiorna da sola ogni 15 minuti.
setlocal
set REPO=EnricoLF/lol-dashboard
set WORKFLOW=aggiorna-dashboard.yml
set PAGE=https://enricolf.github.io/lol-dashboard/

where gh >nul 2>nul || (
  echo GitHub CLI non trovato: apro la pagina con l'ultimo aggiornamento automatico.
  goto open
)

rem L'ultimo avvio manuale prima di questo, per riconoscere quello nuovo
set OLD=
for /f %%i in ('gh run list --repo %REPO% --workflow %WORKFLOW% --event workflow_dispatch --limit 1 --json databaseId --jq ".[0].databaseId"') do set OLD=%%i

echo Avvio l'aggiornamento su GitHub...
gh workflow run %WORKFLOW% --repo %REPO% || goto failed

set RUN=%OLD%
set TRIES=0
:wait
timeout /t 3 /nobreak >nul
for /f %%i in ('gh run list --repo %REPO% --workflow %WORKFLOW% --event workflow_dispatch --limit 1 --json databaseId --jq ".[0].databaseId"') do set RUN=%%i
set /a TRIES+=1
if "%RUN%"=="%OLD%" if %TRIES% lss 20 goto wait
if "%RUN%"=="%OLD%" goto failed

echo Aggiornamento in corso, di solito dura meno di un minuto...
gh run watch %RUN% --repo %REPO% --exit-status --interval 5 >nul || goto failed
echo Fatto. Se la pagina mostra ancora i dati vecchi, ricaricala con Ctrl+F5.
goto open

:failed
echo.
echo Aggiornamento non riuscito. Dettagli: https://github.com/%REPO%/actions
pause

:open
start "" %PAGE%
