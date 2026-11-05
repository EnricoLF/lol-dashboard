# Dati della dashboard

Questo ramo contiene solo i dati, aggiornati dal workflow "Aggiorna dashboard" a ogni esecuzione:

- `data/lp_history.json`: rank e LP a ogni cambiamento (non si può ricostruire: Riot non fornisce lo storico);
- `data/state.json`: Riot ID e PUUID degli account;
- `data/benchmarks.json`: medie per lega, generate a mano con `build_benchmarks.py`;
- `.cache_matches/`: le partite già scaricate da Riot.

Non modificarlo a mano mentre il workflow è attivo. Il codice è nel ramo `main`.
