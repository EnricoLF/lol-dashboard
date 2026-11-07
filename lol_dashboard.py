#!/usr/bin/env python3
"""
LoL Dashboard
=============
Genera una dashboard HTML (output/dashboard.html) con le statistiche Ranked Solo/Duo:
confronto Solo vs Duo Queue, andamento win rate e LP, campioni, ruoli, orari migliori,
obiettivi, abitudini (tilt, sessioni) e ultime partite.

Pensato per essere eseguito in automatico piu' volte al giorno (vedi refresh_dashboard.bat):
  - scarica solo le partite nuove: lo storico si accumula nella cache locale;
  - salva il rank ogni volta che cambia, da cui nascono il grafico e gli LP di ogni partita;
  - se l'API Key e' scaduta rigenera comunque la dashboard dai dati in cache;
  - lanciato a mano in un terminale con la key scaduta, ne chiede una nuova e la salva nel .env.

Dal 5/11/2026 lo esegue GitHub Actions ogni 15 minuti (.github/workflows/aggiorna-dashboard.yml) e pubblica
le pagine su GitHub Pages; aggiorna_dashboard.bat avvia lo stesso workflow su richiesta.

Gestisce piu' account in un'unica pagina (output/dashboard.html): un selettore in alto cambia
account all'istante, e ogni account ha il suo indirizzo (dashboard.html#<account>) per aprirlo in
un'altra finestra. output/index.html riassume tutti gli account.

Uso:
    python lol_dashboard.py                              # usa RIOT_IDS / RIOT_REGION dal file .env
    python lol_dashboard.py --riot-id "Nome#TAG" --riot-id "Altro#EUW" --count 300 --open
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import webbrowser
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import requests

from lol_duo_analyzer import (
    API_KEY,
    DEFAULT_DUO_GAMES,
    RANKED_SOLO_DUO_QUEUE,
    REGIONS,
    SCRIPT_DIR,
    AuthError,
    NotFoundError,
    RiotAPIError,
    RiotClient,
    classify_games,
    detect_duo_partners,
    extract_game,
    load_dotenv,
    parse_riot_id,
)
from lol_level import estimate_level
from lol_metrics import (BENCHMARK_VERSION, PING_TYPES, ally_protection, build_distribution, cs_at_10, match_duration_minutes,
                         objective_share, participant_metrics, ping_counts, team_positions)

CACHE_DIR = SCRIPT_DIR / ".cache_matches"
ENV_FILE = SCRIPT_DIR / ".env"
DATA_DIR = SCRIPT_DIR / "data"
STATE_FILE = DATA_DIR / "state.json"        # Riot ID -> PUUID, per lavorare offline se la key e' scaduta
LP_FILE = DATA_DIR / "lp_history.json"      # Riot ID -> rank a ogni cambiamento (fino al 4/11/2026: uno al giorno)
BENCHMARK_FILE = DATA_DIR / "benchmarks.json"  # medie per lega e ruolo, generate da build_benchmarks.py
TEMPLATE_FILE = SCRIPT_DIR / "dashboard_template.html"
INDEX_TEMPLATE_FILE = SCRIPT_DIR / "index_template.html"
DATA_PLACEHOLDER = "/*__DASHBOARD_DATA__*/null"


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def platform_from_match_id(match_id: str) -> str:
    """"EUW1_7123456789" -> "euw1": il prefisso del match e' la piattaforma per League-V4."""
    return match_id.split("_", 1)[0].lower()


def is_interactive() -> bool:
    """True se lo script gira in una finestra (doppio clic), False se e' l'attivita' pianificata con l'output su file."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def save_api_key(key: str) -> None:
    """Scrive RIOT_API_KEY nel file .env (sostituendo la riga esistente) lasciando invariato il resto."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.is_file() else []
    new_line = f"RIOT_API_KEY={key}"
    for i, line in enumerate(lines):
        if line.strip().startswith("RIOT_API_KEY="):
            lines[i] = new_line
            break
    else:
        lines.append(new_line)
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ["RIOT_API_KEY"] = key


def ensure_api_key(client: RiotClient | None, region: str, account: dict) -> RiotClient | None:
    """Verifica la API Key con una richiesta leggera. Se e' scaduta e lo script gira in una finestra,
    ne chiede una nuova, la prova e, se funziona, la salva nel .env.

    Senza finestra (attivita' pianificata) o se l'utente non inserisce nulla restituisce il client com'e':
    la dashboard verra' rigenerata con i dati in cache.
    """
    new_key = None
    while True:
        if client is not None:
            try:
                client.get_puuid(account["name"], account["tag"])
            except AuthError:
                problem = "La API Key e' scaduta o non valida."
            except RiotAPIError:
                return client  # rete, Riot ID sbagliato...: li gestisce update_account
            else:
                if new_key:
                    save_api_key(new_key)
                    print("Chiave valida, salvata nel file .env.")
                return client
        else:
            problem = "API Key mancante."

        if not is_interactive():
            return client
        if new_key:
            problem = "Anche questa chiave non funziona."
        print(f"\n{problem}\nGenerane una nuova su https://developer.riotgames.com (pulsante 'Regenerate API Key'), "
              "incollala qui e premi Invio.\nPremi solo Invio per usare i dati gia' in cache.")
        try:
            new_key = input("Nuova API Key: ").strip().strip('"').strip("'")
        except EOFError:
            return client
        if not new_key:
            return client
        if not new_key.startswith("RGAPI-"):
            print("La chiave deve iniziare con RGAPI-. Riprova.")
            new_key = None
            continue
        client = RiotClient(new_key, region, CACHE_DIR)


def latest_ddragon_version() -> str | None:
    """Versione corrente di Data Dragon (serve solo per le icone dei campioni, nessuna key)."""
    try:
        return requests.get("https://ddragon.leagueoflegends.com/api/versions.json", timeout=5).json()[0]
    except (requests.RequestException, ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Download e rank
# ---------------------------------------------------------------------------

def download_new_matches(client: RiotClient, match_ids: list[str]) -> None:
    """Scarica in cache i match non ancora presenti. Gli altri sono gia' su disco."""
    new_ids = [m for m in match_ids if not (CACHE_DIR / f"{m}.json").is_file()]
    print(f"{len(match_ids)} match recenti, {len(new_ids)} nuovi da scaricare.")
    interactive = sys.stdout.isatty()
    for i, match_id in enumerate(new_ids, 1):
        if interactive:
            print(f"\r  Scarico match {i}/{len(new_ids)} ({match_id})   ", end="", flush=True)
        elif i % 10 == 0 or i == len(new_ids):
            print(f"  Scaricati {i}/{len(new_ids)}")
        try:
            client.get_match(match_id)
        except AuthError:
            raise
        except RiotAPIError as exc:
            print(f"\n  [salto {match_id}] {exc}")
    if interactive and new_ids:
        print()


def fetch_rank(client: RiotClient, platform: str, puuid: str) -> dict | None:
    """Rank Solo/Duo attuale, o None se la richiesta fallisce (il rank e' facoltativo)."""
    try:
        entries = client.get_ranked_entries(platform, puuid)
    except AuthError:
        raise
    except RiotAPIError as exc:
        print(f"  [rank non disponibile] {exc}")
        return None
    entry = next((e for e in entries if e.get("queueType") == "RANKED_SOLO_5x5"), None)
    if entry is None:
        return {"tier": "UNRANKED", "rank": "", "lp": 0, "wins": 0, "losses": 0}
    return {
        "tier": entry.get("tier", "UNRANKED"),
        "rank": entry.get("rank", ""),
        "lp": entry.get("leaguePoints", 0),
        "wins": entry.get("wins", 0),
        "losses": entry.get("losses", 0),
    }


RANK_FIELDS = ("tier", "rank", "lp", "wins", "losses")
TIERS = ("IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER")
DIVISIONS = ("IV", "III", "II", "I")
LP_STATS_GAMES = 20   # LP medi calcolati sulle ultime 20 vittorie e sulle ultime 20 sconfitte


def record_lp_snapshot(history: list[dict], rank: dict) -> None:
    """Aggiunge un punto ogni volta che il rank cambia, cioe' di solito dopo ogni partita.

    "time" e' il primo aggiornamento in cui lo script ha visto questo rank, "seen" l'ultimo.
    I punti salvati fino al 4/11/2026 hanno solo "date" (allora se ne teneva uno al giorno).
    """
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    last = history[-1] if history else None
    if last and all(last.get(k) == rank.get(k) for k in RANK_FIELDS):
        last["seen"] = now
        return
    history.append({"date": date.today().isoformat(), "time": now, "seen": now, **rank})


def ladder_lp(rank: dict) -> int | None:
    """LP su un'unica scala, come ladder() nel template: 400 per tier, 100 per divisione, da Master in su conta solo l'LP."""
    tier = rank.get("tier")
    if tier not in TIERS:
        return None
    if TIERS.index(tier) >= TIERS.index("MASTER"):
        return TIERS.index("MASTER") * 400 + rank.get("lp", 0)
    if rank.get("rank") not in DIVISIONS:
        return None
    return TIERS.index(tier) * 400 + DIVISIONS.index(rank["rank"]) * 100 + rank.get("lp", 0)


def snapshot_ms(snapshot: dict, field: str) -> float | None:
    value = snapshot.get(field)
    return datetime.fromisoformat(value).timestamp() * 1000 if value else None


def lp_changes(history: list[dict], games: list[dict], end_times: dict[str, int]) -> tuple[dict[str, int], list[dict]]:
    """LP guadagnati o persi, ricavati dai cambi di rank.

    Riot non dice quanti LP vale una partita: si ricava confrontando due rank consecutivi. Se tra i due
    vittorie + sconfitte sono aumentate di 1 e nell'intervallo di tempo e' finita esattamente una partita
    in cache, con lo stesso esito, la variazione di LP e' di quella partita.
    Restituisce (match_id -> LP, blocchi). Un blocco e' un intervallo in cui le partite sono tutte vinte
    o tutte perse, {"win", "games", "lp"}: serve per le medie anche quando l'intervallo ha piu' partite.
    """
    ended = [(end_times.get(g["id"]) or g["ts"] + g["dur"] * 60000, g) for g in games]
    per_match, blocks = {}, []
    for before, after in zip(history, history[1:]):
        played = (after["wins"] + after["losses"]) - (before["wins"] + before["losses"])
        won = after["wins"] - before["wins"]
        lp_before, lp_after = ladder_lp(before), ladder_lp(after)
        # nessuna partita (es. dodge), nuova stagione o partite di piazzamento: niente da attribuire
        if played <= 0 or not 0 <= won <= played or lp_before is None or lp_after is None:
            continue
        delta = lp_after - lp_before
        if won in (0, played):
            blocks.append({"win": won == played, "games": played, "lp": delta})
        start = snapshot_ms(before, "time") or snapshot_ms(before, "seen")
        end = snapshot_ms(after, "time")
        if played == 1 and start and end:
            inside = [g for t, g in ended if start < t <= end]
            if len(inside) == 1 and inside[0]["win"] == (won == 1):
                per_match[inside[0]["id"]] = delta
    return per_match, blocks


def season_start(games: list[dict], rank: dict | None, patches: dict[str, str]) -> int | None:
    """Inizio (timestamp ms della prima partita) della stagione ranked in corso, o None se non ne ha giocate.

    Riot azzera vittorie e sconfitte a ogni nuova stagione, quindi le ultime wins + losses partite (games va
    dalla piu' recente) sono quelle della stagione. Il conteggio puo' scostarsi di qualche partita, per esempio
    se l'ultima non e' ancora scaricabile. Una stagione pero' parte sempre con una patch nuova: se il confine
    cade tra le partite in archivio, lo si sposta sul cambio di patch piu' vicino, entro 3 partite.
    """
    played = (rank or {}).get("wins", 0) + (rank or {}).get("losses", 0)
    if not games or not played:
        return None
    if played >= len(games):
        return games[-1]["ts"]   # tutte le partite in archivio sono della stagione in corso

    def patch(i: int) -> str | None:
        return patches.get(games[i]["id"])

    changes = [i for i in range(max(1, played - 3), min(len(games), played + 4)) if patch(i) != patch(i - 1)]
    boundary = min(changes, key=lambda i: abs(i - played)) if changes else played
    return games[boundary - 1]["ts"]


def lp_stats(blocks: list[dict]) -> dict:
    """LP medi per vittoria e per sconfitta, sulle ultime LP_STATS_GAMES partite di ciascun tipo."""
    stats = {}
    for label, outcome in (("win", True), ("loss", False)):
        games = total = 0
        for block in reversed(blocks):
            if block["win"] == outcome:
                games += block["games"]
                total += block["lp"]
                if games >= LP_STATS_GAMES:
                    break
        stats[label] = {"avg": round(total / games, 1), "games": games} if games else None
    return stats


# ---------------------------------------------------------------------------
# Estrazione statistiche
# ---------------------------------------------------------------------------

def load_cached_matches(puuid: str) -> list[dict]:
    """Tutti i match Ranked Solo/Duo in cache giocati da `puuid`, dal piu' recente."""
    matches = []
    for path in CACHE_DIR.glob("*.json"):
        match = load_json(path, None)
        if not match:
            continue
        info = match.get("info", {})
        if info.get("queueId") != RANKED_SOLO_DUO_QUEUE:
            continue
        if puuid not in match.get("metadata", {}).get("participants", []):
            continue
        matches.append(match)
    matches.sort(key=lambda m: m["info"].get("gameStartTimestamp") or m["info"].get("gameCreation", 0),
                 reverse=True)
    return matches


def extract_extras(match: dict, puuid: str) -> dict:
    """Statistiche aggiuntive non presenti in GameRecord: contesto di squadra, obiettivi, challenges."""
    info = match["info"]
    me = next(p for p in info["participants"] if p.get("puuid") == puuid)
    team = [p for p in info["participants"] if p.get("teamId") == me.get("teamId")]
    team_kills = sum(p.get("kills", 0) for p in team)
    team_damage = sum(p.get("totalDamageDealtToChampions", 0) for p in team)
    challenges = me.get("challenges") or {}
    objectives = next((t.get("objectives", {}) for t in info.get("teams", [])
                       if t.get("teamId") == me.get("teamId")), {})

    def first(objective: str) -> bool:
        return bool(objectives.get(objective, {}).get("first"))

    return {
        "queueId": info.get("queueId"),
        "gold": me.get("goldEarned", 0),
        "kp": (me.get("kills", 0) + me.get("assists", 0)) / team_kills if team_kills else None,
        "dmgShare": me.get("totalDamageDealtToChampions", 0) / team_damage if team_damage else 0,
        "cs10": cs_at_10(me, match_duration_minutes(info)),
        "soloKills": challenges.get("soloKills", 0),
        "wards": me.get("wardsPlaced", 0),
        "ctrl": me.get("detectorWardsPlaced", 0),
        "objDmg": me.get("damageDealtToObjectives", 0),
        "allyHeal": ally_protection(me),          # cure e scudi ai compagni (impatto in combattimento)
        "objPart": objective_share(info, me),     # partecipazione a torri, draghi e Baroni
        "turrets": me.get("turretTakedowns", 0),
        "dead": me.get("totalTimeSpentDead", 0),
        "dbl": me.get("doubleKills", 0),
        "tpl": me.get("tripleKills", 0),
        "qdr": me.get("quadraKills", 0),
        "pnt": me.get("pentaKills", 0),
        "fbMe": bool(me.get("firstBloodKill") or me.get("firstBloodAssist")),
        "side": "Blu" if me.get("teamId") == 100 else "Rosso",
        "teamFb": first("champion"),
        "firstTower": first("tower"),
        "firstDragon": first("dragon"),
        "firstGrubs": first("horde"),
        "firstBaron": first("baron"),
        **{k: v for k, v in ping_counts(me).items() if k not in PING_TYPES},
        "pings": None if "assistMePings" not in me else {t: me.get(t, 0) for t in PING_TYPES},
        **lane_opponent(info, me),
    }


def lane_opponent(info: dict, me: dict) -> dict:
    """Confronto con l'avversario diretto: il giocatore nemico con lo stesso ruolo (teamPosition).

    "lane" viene dalla challenge Riot laningPhaseGoldExpAdvantage, che vale 1 per chi chiude la fase di
    corsia (14 minuti) con almeno il 20% di oro ed esperienza in piu' dell'avversario:
    1 = corsia vinta, -1 = persa, 0 = equilibrata, None = dato non disponibile.
    """
    role = me.get("teamPosition")
    opp = next((p for p in info["participants"]
                if role and p.get("teamId") != me.get("teamId") and p.get("teamPosition") == role), None)
    if opp is None:
        return {"oppChamp": None}
    mine, theirs = me.get("challenges") or {}, opp.get("challenges") or {}

    lane = None
    if "laningPhaseGoldExpAdvantage" in mine and "laningPhaseGoldExpAdvantage" in theirs:
        lane = (1 if mine["laningPhaseGoldExpAdvantage"] else 0) - (1 if theirs["laningPhaseGoldExpAdvantage"] else 0)

    def challenge(source: dict, key: str) -> int | None:
        return round(source[key]) if key in source else None

    minutes = match_duration_minutes(info)
    my_cs10, opp_cs10 = cs_at_10(me, minutes), cs_at_10(opp, minutes)
    return {
        "oppChamp": opp.get("championName", "?"),
        "lane": lane,
        "cs10Diff": None if None in (my_cs10, opp_cs10) else my_cs10 - opp_cs10,
        # massimo vantaggio raggiunto durante la partita, tuo e dell'avversario (sempre >= 0)
        "csLead": challenge(mine, "maxCsAdvantageOnLaneOpponent"),
        "csDeficit": challenge(theirs, "maxCsAdvantageOnLaneOpponent"),
        "lvlLead": challenge(mine, "maxLevelLeadLaneOpponent"),
        "lvlDeficit": challenge(theirs, "maxLevelLeadLaneOpponent"),
        # differenze a fine partita: dipendono anche dall'esito
        "goldDiff": me.get("goldEarned", 0) - opp.get("goldEarned", 0),
        "dmgDiff": me.get("totalDamageDealtToChampions", 0) - opp.get("totalDamageDealtToChampions", 0),
        "visDiff": me.get("visionScore", 0) - opp.get("visionScore", 0),
    }


def build_games(matches: list[dict], puuid: str, duo_games: int) -> tuple[list[dict], list[dict], list[str]]:
    """Restituisce (partite per la dashboard, partner duo, nomi dei compagni).

    I compagni sono salvati come indici nella lista dei nomi per tenere leggero l'HTML.
    """
    pairs = []
    for match in matches:
        record = extract_game(match, puuid)
        if record:
            pairs.append((record, extract_extras(match, puuid)))

    records = [r for r, _ in pairs]
    partners = detect_duo_partners(records, duo_games)
    classify_games(records, partners)

    player_index: dict[str, int] = {}
    players: list[str] = []
    games = []
    for record, extras in pairs:
        mates = []
        for mate_puuid, name in record.teammates.items():
            if mate_puuid not in player_index:
                player_index[mate_puuid] = len(players)
                players.append(name)
            mates.append(player_index[mate_puuid])
        games.append({
            "id": record.match_id,
            "ts": record.game_start,
            "dur": round(record.duration_min, 2),
            "champ": record.champion,
            "role": record.position,
            "win": record.win,
            "k": record.kills,
            "d": record.deaths,
            "a": record.assists,
            "cs": record.cs,
            "dmg": record.damage_to_champions,
            "taken": record.damage_taken,
            "mitig": record.damage_self_mitigated,
            "vis": record.vision_score,
            "queue": record.category,
            "partners": record.partners,
            "mates": mates,
            **extras,
        })

    partner_list = [{"name": name, "games": n} for name, n in partners.values()]
    return games, partner_list, players


# ---------------------------------------------------------------------------
# Generazione pagine
# ---------------------------------------------------------------------------

def render_template(template: Path, payload: dict, output: Path) -> None:
    # "</" viene spezzato perche' i nomi dei giocatori arrivano dall'API: un nome contenente
    # "</script>" non deve poter chiudere il tag che contiene i dati.
    data_js = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(template.read_text(encoding="utf-8").replace(DATA_PLACEHOLDER, data_js), encoding="utf-8")


DASHBOARD_FILE = "dashboard.html"


def account_slug(game_name: str, tag_line: str) -> str:
    """Identificativo dell'account nell'indirizzo della pagina, es. dashboard.html#duroduro_bwc_dur1."""
    return re.sub(r"[^\w\-]+", "_", f"{game_name}_{tag_line}").strip("_").lower()


def update_account(account: dict, client: RiotClient | None, args: argparse.Namespace,
                   state: dict, lp_history: dict, tiers: dict, calibration: dict | None = None) -> tuple[dict, dict] | None:
    """Aggiorna un account e ne prepara i dati.

    Restituisce (dati per la dashboard, riepilogo per la pagina indice), o None se non ci sono dati.
    """
    riot_id, key = account["player"], account["key"]
    puuid = state.get(key)
    status = {"ok": True, "message": ""}
    print(f"\n[{datetime.now():%Y-%m-%d %H:%M}] Aggiornamento {riot_id}")

    try:
        if client is None:
            raise AuthError("API Key mancante: impostala nel file .env (RIOT_API_KEY=RGAPI-...).")
        puuid = client.get_puuid(account["name"], account["tag"])
        state[key] = puuid
        save_json(STATE_FILE, state)

        match_ids = client.get_match_ids(puuid, args.count)
        download_new_matches(client, match_ids)

        if match_ids:
            rank = fetch_rank(client, platform_from_match_id(match_ids[0]), puuid)
            if rank:
                record_lp_snapshot(lp_history.setdefault(key, []), rank)
                save_json(LP_FILE, lp_history)
    except NotFoundError:
        print(f"Account '{riot_id}' non trovato. Controlla nome, tag e regione.")
        return None
    except RiotAPIError as exc:
        status = {"ok": False, "message": f"Aggiornamento non riuscito il {datetime.now():%d/%m %H:%M}: {exc}"}
        print(status["message"])
        if not puuid:
            print("Nessun dato in cache per questo account: impossibile generare la dashboard.")
            return None
        print("Genero la dashboard con i dati gia' in cache.")

    matches = load_cached_matches(puuid)
    games, partners, players = build_games(matches, puuid, args.duo_games)
    if not games:
        print("Nessuna partita Ranked Solo/Duo valida da mostrare.")
        return None

    # Riferimento "lobby": gli altri 9 giocatori delle tue partite, cioe' giocatori del tuo stesso MMR
    lobby_rows = [m for match in matches for p in match["info"]["participants"]
                  if p.get("puuid") != puuid and (m := participant_metrics(match, p))]
    lobby = {"label": "La tua lobby", "matches": len(matches), "roles": build_distribution(lobby_rows)}

    # Posizione nella squadra (1 = il migliore dei 5), rispetto alle distribuzioni della lobby
    team_pos = team_positions(matches, puuid, lobby["roles"])
    for game in games:
        position = team_pos.get(game["id"])
        if position:
            game["teamRank"] = position["rank"]
            if "rank10" in position:
                game["rank10"] = position["rank10"]
            if "badge" in position:
                game["badge"] = position["badge"]

    history = lp_history.get(key, [])
    end_times = {m["metadata"].get("matchId"): m["info"].get("gameEndTimestamp") for m in matches}
    lp_by_match, lp_blocks = lp_changes(history, games, end_times)
    for game in games:
        if game["id"] in lp_by_match:
            game["lp"] = lp_by_match[game["id"]]

    patches = {m["metadata"].get("matchId"): ".".join(m["info"].get("gameVersion", "").split(".")[:2]) for m in matches}
    start = season_start(games, history[-1] if history else None, patches)
    season_games = [g for g in games if g["ts"] >= start] if start is not None else []
    season = {"start": start, "games": len(season_games)} if season_games else None
    if season:
        print(f"Stagione in corso: {len(season_games)} partite dal {datetime.fromtimestamp(start / 1000):%d/%m/%Y}")

    # Livello stimato nel ruolo principale: stagione in corso, oppure le ultime 100 partite se nella
    # stagione le partite in quel ruolo sono poche
    level = None
    for scope, pool in (("season", season_games), ("recent", games[:100])):
        level_ids = {g["id"] for g in pool}
        level_rows = []
        for m in matches:
            match_id = m["metadata"].get("matchId")
            me = next((p for p in m["info"]["participants"] if p.get("puuid") == puuid), None)
            if match_id in level_ids and me and (row := participant_metrics(m, me)):
                position = team_pos.get(match_id)
                level_rows.append({**row, "team": position and position["beat"], "teamRank": position and position["rank"]})
        level = estimate_level(level_rows, tiers, history[-1] if history else None, lobby["roles"])
        if level:
            level["scope"] = scope
            # quante volte un giocatore del tuo ruolo e' in media il migliore della squadra (non sempre il 20%)
            level["teamBestExpected"] = ((calibration or {}).get("teamBest") or {}).get(level["role"])
            break
    if level:
        verdicts = {"above": "statistiche sopra il rank", "below": "statistiche sotto il rank", "inline": "statistiche in linea con il rank"}
        print(f"Livello stimato da {level['role']} ({level['games']} partite): {level['label']} "
              f"({level['lowLabel']} - {level['highLabel']}), {verdicts.get(level['verdict'], 'rank non disponibile')}, "
              f"win rate {level['winRate']:.0%} ({level['wrLow']:.0%}-{level['wrHigh']:.0%})")
    generated_at = datetime.now().isoformat(timespec="seconds")
    payload = {
        "slug": account["slug"],
        "player": riot_id,
        "generatedAt": generated_at,
        "status": status,
        "rank": history[-1] if history else None,
        "lpHistory": history,
        "lpStats": lp_stats(lp_blocks),
        "season": season,
        "level": level,
        "lobby": lobby,
        "partners": partners,
        "players": players,
        "games": games,
    }
    print(f"Dati pronti: {len(games)} partite, {len(partners)} partner duo")

    # Riepilogo per la pagina indice: stagione in corso se nota, nel ruolo principale (quello della stima
    # del livello, altrimenti il piu' giocato); le partite sono gia' dalla piu' recente
    shown = season_games or games
    main_role = level["role"] if level else Counter(g["role"] for g in shown).most_common(1)[0][0]
    shown = [g for g in shown if g["role"] == main_role] or shown
    recent = shown[:20]
    champion_counts = Counter(g["champ"] for g in shown)
    return payload, {
        "player": riot_id,
        "slug": account["slug"],
        "file": f"{DASHBOARD_FILE}#{account['slug']}",
        "generatedAt": generated_at,
        "status": status,
        "rank": history[-1] if history else None,
        "season": bool(season_games),
        "role": main_role,
        "level": level and {k: level.get(k) for k in ("label", "band", "lowLabel", "highLabel", "verdict", "gap", "farmSignal", "tier", "scope", "role", "games", "teamRank",
                      "teamScore", "teamBest", "teamBestExpected", "position", "low", "high", "rankPosition", "winRate", "wrLow", "wrHigh", "resultSignal")},
        "games": len(shown),
        "winRate": sum(g["win"] for g in shown) / len(shown),
        "recentWinRate": sum(g["win"] for g in recent) / len(recent),
        "recentGames": len(recent),
        "lastGame": games[0]["ts"],
        "champions": [champ for champ, _ in champion_counts.most_common(3)],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def split_riot_ids(value: str | None) -> list[str]:
    """"Nome#TAG; Altro#EUW" -> ["Nome#TAG", "Altro#EUW"] (separatori ammessi: ; e ,)."""
    return [part.strip() for part in re.split(r"[;,]", value or "") if part.strip()]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Genera le dashboard HTML delle Ranked Solo/Duo per uno o piu' account.")
    parser.add_argument("--riot-id", action="append", dest="riot_ids",
                        help='Riot ID, es. "Nome#TAG". Ripetibile. Default: RIOT_IDS nel file .env')
    parser.add_argument("--region", choices=REGIONS, default=os.environ.get("RIOT_REGION", "europe"),
                        help="Cluster regionale (default: RIOT_REGION nel .env, altrimenti europe)")
    parser.add_argument("--count", type=int, default=100,
                        help="Match recenti da controllare a ogni aggiornamento (default: 100). "
                             "Al primo avvio puoi usare un valore piu' alto per recuperare piu' storico.")
    parser.add_argument("--duo-games", type=int, default=DEFAULT_DUO_GAMES,
                        help=f"Partite insieme per considerare un compagno partner duo (default: {DEFAULT_DUO_GAMES})")
    parser.add_argument("--api-key", help="Riot API Key (in alternativa: RIOT_API_KEY nel .env)")
    parser.add_argument("--output-dir", type=Path, default=SCRIPT_DIR / "output",
                        help="Cartella delle dashboard (default: ./output)")
    parser.add_argument("--open", action="store_true", help="Apre la dashboard nel browser alla fine")
    args = parser.parse_args()
    if not args.riot_ids:
        # RIOT_ID (singolo) resta supportato per compatibilita' con i .env precedenti
        args.riot_ids = split_riot_ids(os.environ.get("RIOT_IDS") or os.environ.get("RIOT_ID"))
    if args.count < 1:
        parser.error("--count deve essere almeno 1")
    if args.duo_games < 2:
        parser.error("--duo-games deve essere almeno 2")
    return args


def current_benchmarks(tier_file: dict) -> tuple[dict, dict | None, list[str]]:
    """Leghe e calibrazione di benchmarks.json calcolate con le formule attuali, e leghe da rigenerare.

    Una lega calcolata con formule diverse (es. il vecchio indice di impatto) darebbe punteggi sbagliati
    senza che nulla lo segnali: meglio non usarla e dirlo.
    """
    tiers = {t: v for t, v in tier_file.get("tiers", {}).items() if v.get("version") == BENCHMARK_VERSION}
    stale = [t for t in tier_file.get("tiers", {}) if t not in tiers]
    calibration = tier_file.get("calibration")
    if not calibration or calibration.get("version") != BENCHMARK_VERSION:
        calibration = None
    if stale:
        print(f"Medie per lega calcolate con formule precedenti, non usate: {', '.join(stale)}. "
              "Rigenerale con: python build_benchmarks.py --recompute")
    return tiers, calibration, stale


def main() -> int:
    sys.stdout.reconfigure(errors="replace")
    load_dotenv(SCRIPT_DIR / ".env")  # prima di parse_args, che legge i default dal .env
    args = parse_args()

    accounts = []
    for raw in args.riot_ids:
        try:
            game_name, tag_line = parse_riot_id(raw)
        except ValueError as exc:
            print(exc)
            continue
        riot_id = f"{game_name}#{tag_line}"
        if any(a["key"] == riot_id.lower() for a in accounts):
            continue
        accounts.append({"name": game_name, "tag": tag_line, "player": riot_id,
                         "key": riot_id.lower(), "slug": account_slug(game_name, tag_line)})
    if not accounts:
        print('Nessun Riot ID valido: aggiungi RIOT_IDS=Nome#TAG; Altro#TAG nel file .env '
              'oppure usa --riot-id "Nome#TAG".')
        return 1

    api_key = (args.api_key or os.environ.get("RIOT_API_KEY") or API_KEY).strip()
    client = RiotClient(api_key, args.region, CACHE_DIR) if api_key else None
    state = load_json(STATE_FILE, {})
    lp_history = load_json(LP_FILE, {})
    tier_file = load_json(BENCHMARK_FILE, {})
    tiers, calibration, stale_tiers = current_benchmarks(tier_file)
    ddragon = latest_ddragon_version()

    payloads, summaries = [], []
    try:
        client = ensure_api_key(client, args.region, accounts[0])
        for account in accounts:
            result = update_account(account, client, args, state, lp_history, tiers, calibration)
            if result:
                payloads.append(result[0])
                summaries.append(result[1])
    except KeyboardInterrupt:
        print("\nInterrotto dall'utente.")
        return 130

    # Un'unica pagina con tutti gli account: le medie per lega sono comuni e salvate una volta sola
    if payloads:
        dashboard = args.output_dir / DASHBOARD_FILE
        render_template(TEMPLATE_FILE, {
            "generatedAt": datetime.now().isoformat(timespec="seconds"),
            "region": args.region,
            "duoGames": args.duo_games,
            "ddragon": ddragon,
            "benchmarkDate": tier_file.get("generatedAt"),
            "staleTiers": stale_tiers,
            # scarto vittoria - sconfitta di ogni parametro e quota "migliore della squadra" per ruolo
            "calibration": calibration and {k: calibration[k] for k in ("outcomeGap", "teamBest")},
            "tierBenchmarks": {
                tier: {"label": "Master+" if tier == "MASTER" else tier.capitalize(), "matches": t["matches"], "roles": t["roles"]}
                for tier, t in tiers.items()
            },
            "accounts": payloads,
        }, dashboard)
        print(f"\nDashboard aggiornata ({len(payloads)} account): {dashboard}")
        # le vecchie pagine separate per account non sono piu' usate
        for old in args.output_dir.glob("dashboard_*.html"):
            old.unlink()

    index = args.output_dir / "index.html"
    shown = {s["player"] for s in summaries}
    render_template(INDEX_TEMPLATE_FILE, {
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "ddragon": ddragon,
        "accounts": summaries,
        "missing": [a["player"] for a in accounts if a["player"] not in shown],
    }, index)
    print(f"Indice account: {index}")

    if args.open:
        webbrowser.open((args.output_dir / DASHBOARD_FILE).resolve().as_uri())
    if not summaries:
        return 1
    return 0 if len(summaries) == len(accounts) and all(s["status"]["ok"] for s in summaries) else 2


if __name__ == "__main__":
    sys.exit(main())


