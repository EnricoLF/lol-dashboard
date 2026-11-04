@echo off
rem Aggiorna la dashboard LoL. Pensato per l'Utilita' di pianificazione di Windows (una volta al giorno).
rem Riot ID, regione e API Key vengono letti dal file .env nella stessa cartella.
cd /d "%~dp0"
if not exist output mkdir output
python lol_dashboard.py %* >> "output\refresh.log" 2>&1
