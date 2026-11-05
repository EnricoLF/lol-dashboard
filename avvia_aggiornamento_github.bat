@echo off
rem Chiede a GitHub di aggiornare subito la dashboard: avvia il workflow "Aggiorna dashboard".
rem Lo usa l'attivita' pianificata di Windows "LoL Dashboard Refresh" (allo sblocco, all'accesso e
rem ogni 15 minuti) come riserva allo scheduler di GitHub, che non e' affidabile.
rem I dati restano solo su GitHub: questo file non aggiorna nulla sul PC.
rem Serve GitHub CLI (gh) collegato all'account EnricoLF. Esito in output\trigger.log.
setlocal
cd /d "%~dp0"
if not exist output mkdir output
set GH=gh
if exist "%ProgramFiles%\GitHub CLI\gh.exe" set GH="%ProgramFiles%\GitHub CLI\gh.exe"
echo [%date% %time%] avvio del workflow su GitHub>> output\trigger.log
%GH% workflow run aggiorna-dashboard.yml --repo EnricoLF/lol-dashboard >> output\trigger.log 2>&1
if errorlevel 1 (
  echo [%date% %time%] avvio non riuscito>> output\trigger.log
  exit /b 1
)
exit /b 0
