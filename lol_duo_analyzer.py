#!/usr/bin/env python3
"""
LoL Solo vs Duo Analyzer
========================
Analizza le partite Ranked Solo/Duo (queueId 420) di un giocatore tramite le API
ufficiali di Riot Games (Account-V1 + Match-V5) e confronta le prestazioni
giocate in Solo Queue con quelle giocate in Duo Queue.

I partner di duo vengono riconosciuti automaticamente: un compagno di squadra
che compare in almeno il 15% delle partite analizzate e' considerato "Partner Duo".

Uso:
    python lol_duo_analyzer.py --riot-id "Nome#TAG" --region europe --count 100
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

try:
    import requests
except ImportError:
    sys.exit("Libreria 'requests' mancante. Installala con:  pip install requests")

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

# La API Key viene cercata in quest'ordine:
#   1. argomento --api-key
#   2. variabile d'ambiente RIOT_API_KEY
#   3. file .env nella cartella dello script (riga: RIOT_API_KEY=RGAPI-...)
#   4. la costante qui sotto (sconsigliato: rischi di condividerla per sbaglio)
API_KEY = ""  # es. "RGAPI-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

SCRIPT_DIR = Path(__file__).resolve().parent

RANKED_SOLO_DUO_QUEUE = 420
DEFAULT_DUO_THRESHOLD = 0.15   # quota minima di partite condivise per essere "Partner Duo"
MIN_DUO_GAMES = 2              # evita falsi positivi quando si analizzano pochissime partite
REMAKE_MAX_SECONDS = 300       # partite sotto i 5 minuti sono considerate remake
MAX_IDS_PER_REQUEST = 100      # limite massimo del parametro "count" di Match-V5

# Account-V1 e Match-V5 usano i cluster regionali, non le piattaforme (euw1, na1...).
#   europe   -> EUW, EUNE, TR, RU, ME
#   americas -> NA, BR, LAN, LAS
#   asia     -> KR, JP
#   sea      -> OCE, SG, TW, VN...
REGIONS = ("europe", "americas", "asia", "sea")
# Account-V1 non e' servito dal cluster "sea": per quegli account si usa "asia"
ACCOUNT_REGION = {"europe": "europe", "americas": "americas", "asia": "asia", "sea": "asia"}

# Limiti di una Development API Key: 20 richieste/1s e 100 richieste/2min.
# Vengono comunque aggiornati con i valori reali letti dagli header di risposta.
DEV_KEY_RATE_LIMITS = [(20, 1.0), (100, 120.0)]


# ---------------------------------------------------------------------------
# Eccezioni
# ---------------------------------------------------------------------------

class RiotAPIError(Exception):
    """Errore generico restituito dalle API Riot."""


class AuthError(RiotAPIError):
    """API Key mancante, non valida o scaduta (HTTP 401/403)."""


class NotFoundError(RiotAPIError):
    """Risorsa inesistente (HTTP 404), es. Riot ID sbagliato."""


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class RateLimiter:
    """Limitatore lato client a finestre scorrevoli.

    Tiene traccia degli istanti delle ultime richieste e, prima di ognuna,
    attende quanto basta per non superare nessuno dei limiti configurati
    (es. 20 req/1s e 100 req/120s). E' piu' prudente dei bucket fissi usati
    da Riot, quindi in condizioni normali non si riceve mai un HTTP 429.
    """

    def __init__(self, limits: list[tuple[int, float]]):
        self.limits = limits
        self.history: deque[float] = deque()

    def update_from_header(self, header: str | None) -> None:
        """Aggiorna i limiti dal formato Riot "20:1,100:120"."""
        if not header:
            return
        try:
            parsed = [(int(n), float(w)) for n, w in (part.split(":") for part in header.split(","))]
        except ValueError:
            return
        if parsed:
            self.limits = parsed

    def wait(self) -> None:
        while True:
            now = time.monotonic()
            longest_window = max(window for _, window in self.limits)
            while self.history and now - self.history[0] >= longest_window:
                self.history.popleft()

            sleep_for = 0.0
            for limit, window in self.limits:
                recent = [t for t in self.history if now - t < window]
                if len(recent) >= limit:
                    # Bisogna aspettare che la richiesta piu' vecchia "in eccesso" esca dalla finestra
                    sleep_for = max(sleep_for, window - (now - recent[-limit]) + 0.05)

            if sleep_for <= 0:
                self.history.append(now)
                return
            if sleep_for > 2:
                print(f"\n  [rate limit] limite della API Key raggiunto, attendo {sleep_for:.0f}s...")
            time.sleep(sleep_for)


# ---------------------------------------------------------------------------
# Client API Riot
# ---------------------------------------------------------------------------

class RiotClient:
    """Wrapper minimale per Account-V1 e Match-V5 con retry e cache su disco."""

    def __init__(self, api_key: str, region: str, cache_dir: Path | None,
                 max_retries: int = 5, timeout: float = 15.0):
        self.region = region
        self.account_region = ACCOUNT_REGION[region]
        self.cache_dir = cache_dir
        self.max_retries = max_retries
        self.timeout = timeout
        self.limiter = RateLimiter(list(DEV_KEY_RATE_LIMITS))
        self.session = requests.Session()
        self.session.headers.update({"X-Riot-Token": api_key, "Accept": "application/json"})
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

    def _get(self, url: str, params: dict | None = None):
        """GET con gestione di rate limit (429), errori server (5xx) ed errori di rete."""
        for attempt in range(1, self.max_retries + 1):
            self.limiter.wait()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                backoff = 2 ** attempt
                print(f"\n  [rete] {exc.__class__.__name__}, nuovo tentativo tra {backoff}s "
                      f"({attempt}/{self.max_retries})")
                time.sleep(backoff)
                continue

            self.limiter.update_from_header(resp.headers.get("X-App-Rate-Limit"))
            status = resp.status_code

            if status == 200:
                return resp.json()
            if status == 429:
                # Riot indica quanti secondi aspettare nell'header Retry-After
                retry_after = int(resp.headers.get("Retry-After", 10))
                limit_type = resp.headers.get("X-Rate-Limit-Type", "sconosciuto")
                print(f"\n  [429] rate limit ({limit_type}), attendo {retry_after}s...")
                time.sleep(retry_after + 0.5)
                continue
            if status >= 500:
                backoff = 2 ** attempt
                print(f"\n  [{status}] errore lato Riot, nuovo tentativo tra {backoff}s "
                      f"({attempt}/{self.max_retries})")
                time.sleep(backoff)
                continue
            if status in (401, 403):
                raise AuthError(
                    f"HTTP {status}: API Key non valida o scaduta. Le Development Key durano 24 ore: "
                    "rigenerala su https://developer.riotgames.com e riprova."
                )
            if status == 404:
                raise NotFoundError(f"HTTP 404: risorsa non trovata ({url})")
            raise RiotAPIError(f"HTTP {status}: {resp.text[:200]}")

        raise RiotAPIError(f"Troppi tentativi falliti per {url}")

    def get_puuid(self, game_name: str, tag_line: str) -> str:
        url = (f"https://{self.account_region}.api.riotgames.com/riot/account/v1/accounts/"
               f"by-riot-id/{quote(game_name, safe='')}/{quote(tag_line, safe='')}")
        return self._get(url)["puuid"]

    def get_match_ids(self, puuid: str, count: int, queue: int = RANKED_SOLO_DUO_QUEUE) -> list[str]:
        """Restituisce gli ID degli ultimi `count` match (dal piu' recente), paginando a blocchi di 100."""
        url = f"https://{self.region}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids"
        ids: list[str] = []
        while len(ids) < count:
            batch_size = min(MAX_IDS_PER_REQUEST, count - len(ids))
            batch = self._get(url, params={"queue": queue, "start": len(ids), "count": batch_size})
            ids.extend(batch)
            if len(batch) < batch_size:  # non ci sono altre partite
                break
        return ids

    def get_ranked_entries(self, platform: str, puuid: str) -> list[dict]:
        """League-V4: rank attuale. Usa il routing di piattaforma (es. euw1), non quello regionale."""
        url = f"https://{platform}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
        return self._get(url)

    def get_league_entries(self, platform: str, tier: str, division: str, page: int = 1) -> list[dict]:
        """League-V4: giocatori Ranked Solo/Duo di una divisione (Iron-Diamond), ~200 per pagina."""
        url = f"https://{platform}.api.riotgames.com/lol/league/v4/entries/RANKED_SOLO_5x5/{tier}/{division}"
        return self._get(url, params={"page": page})

    def get_master_league(self, platform: str) -> dict:
        """League-V4: classifica Master Ranked Solo/Duo."""
        return self._get(f"https://{platform}.api.riotgames.com/lol/league/v4/masterleagues/by-queue/RANKED_SOLO_5x5")

    def get_match(self, match_id: str) -> dict:
        """Dettagli di un match. I match sono immutabili, quindi vengono salvati in cache su disco."""
        cache_file = self.cache_dir / f"{match_id}.json" if self.cache_dir else None
        if cache_file and cache_file.is_file():
            try:
                return json.loads(cache_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass  # cache corrotta: la riscarichiamo

        url = f"https://{self.region}.api.riotgames.com/lol/match/v5/matches/{match_id}"
        data = self._get(url)
        if cache_file:
            cache_file.write_text(json.dumps(data), encoding="utf-8")
        return data


# ---------------------------------------------------------------------------
# Estrazione dati dai match
# ---------------------------------------------------------------------------

@dataclass
class GameRecord:
    """Statistiche del giocatore analizzato in una singola partita."""
    match_id: str
    game_start: int              # timestamp in millisecondi
    duration_min: float
    champion: str
    position: str
    win: bool
    kills: int
    deaths: int
    assists: int
    cs: int
    damage_to_champions: int
    damage_taken: int
    damage_self_mitigated: int
    vision_score: int
    teammates: dict[str, str] = field(default_factory=dict)  # puuid -> Riot ID
    category: str = "Solo"
    partners: list[str] = field(default_factory=list)


def riot_id_of(participant: dict) -> str:
    name = participant.get("riotIdGameName") or participant.get("summonerName") or "?"
    tag = participant.get("riotIdTagline") or ""
    return f"{name}#{tag}" if tag else name


def extract_game(match: dict, puuid: str) -> GameRecord | None:
    """Estrae le statistiche del giocatore. Restituisce None per i remake o dati mancanti."""
    info = match.get("info", {})
    participants = info.get("participants", [])
    me = next((p for p in participants if p.get("puuid") == puuid), None)
    if me is None:
        return None

    # Dalla patch 11.20 gameDuration e' in secondi; nei match piu' vecchi (senza
    # gameEndTimestamp) era in millisecondi.
    duration_s = info.get("gameDuration", 0)
    if "gameEndTimestamp" not in info:
        duration_s /= 1000
    if duration_s < REMAKE_MAX_SECONDS or me.get("gameEndedInEarlySurrender"):
        return None

    teammates = {
        p["puuid"]: riot_id_of(p)
        for p in participants
        if p.get("teamId") == me.get("teamId") and p.get("puuid") != puuid
    }

    return GameRecord(
        match_id=match.get("metadata", {}).get("matchId", "?"),
        game_start=info.get("gameStartTimestamp") or info.get("gameCreation", 0),
        duration_min=duration_s / 60,
        champion=me.get("championName", "?"),
        position=me.get("teamPosition") or me.get("individualPosition") or "?",
        win=bool(me.get("win")),
        kills=me.get("kills", 0),
        deaths=me.get("deaths", 0),
        assists=me.get("assists", 0),
        cs=me.get("totalMinionsKilled", 0) + me.get("neutralMinionsKilled", 0),
        damage_to_champions=me.get("totalDamageDealtToChampions", 0),
        damage_taken=me.get("totalDamageTaken", 0),
        damage_self_mitigated=me.get("damageSelfMitigated", 0),
        vision_score=me.get("visionScore", 0),
        teammates=teammates,
    )


# ---------------------------------------------------------------------------
# Rilevamento partner duo e classificazione
# ---------------------------------------------------------------------------

def detect_duo_partners(games: list[GameRecord], threshold: float) -> dict[str, tuple[str, int]]:
    """Trova i compagni presenti in almeno `threshold` delle partite.

    Restituisce {puuid: (Riot ID piu' recente, numero di partite insieme)}.
    """
    counts = Counter(puuid for g in games for puuid in g.teammates)
    min_games = max(MIN_DUO_GAMES, threshold * len(games))

    # Le partite sono ordinate dalla piu' recente: il primo nome trovato e' quello attuale
    latest_name: dict[str, str] = {}
    for g in games:
        for puuid, name in g.teammates.items():
            latest_name.setdefault(puuid, name)

    return {
        puuid: (latest_name[puuid], n)
        for puuid, n in counts.most_common()
        if n >= min_games
    }


def classify_games(games: list[GameRecord], partners: dict[str, tuple[str, int]]) -> None:
    """Marca ogni partita come "Duo" se c'e' almeno un partner in squadra, altrimenti "Solo"."""
    for g in games:
        g.partners = [partners[p][0] for p in g.teammates if p in partners]
        g.category = "Duo" if g.partners else "Solo"


# ---------------------------------------------------------------------------
# Metriche
# ---------------------------------------------------------------------------

def _avg(values: list[float]) -> float:
    return sum(values) / len(values)


def _kda(games: list[GameRecord]) -> float:
    # KDA calcolato sui totali, (K+A)/D: piu' robusto della media dei KDA delle singole
    # partite, che esplode nelle partite senza morti.
    deaths = sum(g.deaths for g in games)
    return sum(g.kills + g.assists for g in games) / max(1, deaths)


# (etichetta, funzione di calcolo, formato di stampa, ha senso calcolare la differenza)
METRICS = [
    ("Partite",                lambda gs: len(gs),                                              "{:.0f}", False),
    ("Vittorie",               lambda gs: sum(g.win for g in gs),                               "{:.0f}", False),
    ("Win Rate %",             lambda gs: 100 * _avg([g.win for g in gs]),                      "{:.1f}", True),
    ("Kills medie",            lambda gs: _avg([g.kills for g in gs]),                          "{:.2f}", True),
    ("Deaths medie",           lambda gs: _avg([g.deaths for g in gs]),                         "{:.2f}", True),
    ("Assists medie",          lambda gs: _avg([g.assists for g in gs]),                        "{:.2f}", True),
    ("KDA (K+A)/D",            _kda,                                                            "{:.2f}", True),
    ("CS/min",                 lambda gs: _avg([g.cs / g.duration_min for g in gs]),            "{:.2f}", True),
    ("Danni a campioni/min",   lambda gs: _avg([g.damage_to_champions / g.duration_min for g in gs]), "{:.0f}", True),
    ("Danni subiti medi",      lambda gs: _avg([g.damage_taken for g in gs]),                   "{:.0f}", True),
    ("Danni mitigati medi",    lambda gs: _avg([g.damage_self_mitigated for g in gs]),          "{:.0f}", True),
    ("Vision score medio",     lambda gs: _avg([g.vision_score for g in gs]),                   "{:.1f}", True),
    ("Durata media (min)",     lambda gs: _avg([g.duration_min for g in gs]),                   "{:.1f}", True),
]

DIFF_COLUMN = "Diff Duo-Solo"


def compute_summary(games: list[GameRecord], partners: dict[str, tuple[str, int]]) -> dict[str, dict[str, float | None]]:
    """Calcola le metriche per ogni gruppo: Solo, Duo, differenza e singolo partner.

    Restituisce {nome colonna: {etichetta metrica: valore o None se il gruppo e' vuoto}}.
    """
    groups: dict[str, list[GameRecord]] = {
        "Solo Queue": [g for g in games if g.category == "Solo"],
        "Duo Queue": [g for g in games if g.category == "Duo"],
    }
    if len(partners) > 1:  # con un solo partner la colonna duplicherebbe "Duo Queue"
        for name, _ in partners.values():
            groups[f"Duo con {name}"] = [g for g in games if name in g.partners]

    summary = {
        col: {label: (fn(gs) if gs else None) for label, fn, _, _ in METRICS}
        for col, gs in groups.items()
    }

    solo, duo = summary["Solo Queue"], summary["Duo Queue"]
    diff = {
        label: (duo[label] - solo[label]) if has_diff and None not in (duo[label], solo[label]) else None
        for label, _, _, has_diff in METRICS
    }
    # Inserisce la colonna differenza subito dopo "Duo Queue"
    ordered = {"Solo Queue": solo, "Duo Queue": duo, DIFF_COLUMN: diff}
    ordered.update({k: v for k, v in summary.items() if k not in ordered})
    return ordered


def format_value(value: float | None, fmt: str, signed: bool = False) -> str:
    if value is None:
        return "-"
    text = fmt.format(value)
    return f"+{text}" if signed and value > 0 else text


def summary_rows(summary: dict[str, dict[str, float | None]]) -> list[list[str]]:
    """Tabella (lista di righe) con intestazione, pronta per terminale e markdown."""
    columns = list(summary)
    rows = [["Metrica", *columns]]
    for label, _, fmt, _ in METRICS:
        rows.append([label, *(format_value(summary[c][label], fmt, signed=(c == DIFF_COLUMN))
                              for c in columns)])
    return rows


# ---------------------------------------------------------------------------
# Output: terminale, Markdown, CSV
# ---------------------------------------------------------------------------

def render_text_table(rows: list[list[str]], max_col_width: int = 24) -> str:
    rows = [[cell if len(cell) <= max_col_width else cell[:max_col_width - 1] + "." for cell in row]
            for row in rows]
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = []
    for idx, row in enumerate(rows):
        cells = [row[0].ljust(widths[0])] + [cell.rjust(w) for cell, w in zip(row[1:], widths[1:])]
        lines.append("  ".join(cells))
        if idx == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def render_markdown_table(rows: list[list[str]]) -> str:
    header, *body = rows
    lines = ["| " + " | ".join(header) + " |",
             "|" + "|".join([":---"] + ["---:"] * (len(header) - 1)) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in body]
    return "\n".join(lines)


def write_summary_csv(path: Path, summary: dict[str, dict[str, float | None]]) -> None:
    columns = list(summary)
    with path.open("w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig: Excel legge gli accenti
        writer = csv.writer(f)
        writer.writerow(["Metrica", *columns])
        for label, *_ in METRICS:
            writer.writerow([label, *("" if summary[c][label] is None else round(summary[c][label], 4)
                                      for c in columns)])


def write_matches_csv(path: Path, games: list[GameRecord]) -> None:
    fields = ["match_id", "data", "categoria", "partner", "campione", "ruolo", "vittoria",
              "kills", "deaths", "assists", "cs", "cs_min", "durata_min",
              "danni_campioni", "danni_subiti", "danni_mitigati", "vision_score"]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(fields)
        for g in games:
            writer.writerow([
                g.match_id,
                time.strftime("%Y-%m-%d %H:%M", time.localtime(g.game_start / 1000)),
                g.category, "; ".join(g.partners), g.champion, g.position, int(g.win),
                g.kills, g.deaths, g.assists, g.cs, round(g.cs / g.duration_min, 2),
                round(g.duration_min, 1), g.damage_to_champions, g.damage_taken,
                g.damage_self_mitigated, g.vision_score,
            ])


def build_report_header(riot_id: str, region: str, requested: int, found: int,
                        games: list[GameRecord], partners: dict[str, tuple[str, int]],
                        threshold: float) -> list[str]:
    lines = [
        f"Giocatore:          {riot_id} ({region})",
        f"Match richiesti:    {requested}  |  trovati: {found}  |  analizzati: {len(games)} "
        f"(esclusi remake/errori: {found - len(games)})",
        f"Soglia partner duo: {threshold:.0%} delle partite (min {MIN_DUO_GAMES})",
    ]
    if partners:
        lines.append("Partner duo rilevati:")
        for name, n in partners.values():
            lines.append(f"  - {name}: {n} partite insieme ({n / len(games):.0%})")
    else:
        lines.append("Nessun partner duo rilevato: tutte le partite risultano in Solo Queue.")
    return lines


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_dotenv(path: Path) -> None:
    """Lettore minimale di file .env (evita la dipendenza da python-dotenv)."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def parse_riot_id(riot_id: str) -> tuple[str, str]:
    if "#" not in riot_id:
        raise ValueError(f"Riot ID non valido: '{riot_id}'. Formato atteso: Nome#TAG")
    name, tag = (part.strip() for part in riot_id.rsplit("#", 1))
    if not name or not tag:
        raise ValueError(f"Riot ID non valido: '{riot_id}'. Formato atteso: Nome#TAG")
    return name, tag


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Confronta le tue prestazioni in Solo Queue vs Duo Queue.")
    parser.add_argument("--riot-id", help='Il tuo Riot ID, es. "Faker#KR1" (se omesso viene chiesto)')
    parser.add_argument("--region", choices=REGIONS, default="europe",
                        help="Cluster regionale (default: europe, per EUW/EUNE)")
    parser.add_argument("--count", type=int, default=100, help="Numero di match da analizzare (default: 100)")
    parser.add_argument("--threshold", type=float, default=DEFAULT_DUO_THRESHOLD,
                        help="Quota di partite per considerare un compagno partner duo (default: 0.15)")
    parser.add_argument("--api-key", help="Riot API Key (in alternativa: RIOT_API_KEY o file .env)")
    parser.add_argument("--output-dir", type=Path, default=SCRIPT_DIR / "output",
                        help="Cartella dei report (default: ./output)")
    parser.add_argument("--no-cache", action="store_true", help="Non usare/salvare la cache dei match")
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count deve essere almeno 1")
    if not 0 < args.threshold <= 1:
        parser.error("--threshold deve essere compreso tra 0 e 1 (es. 0.15)")
    return args


def main() -> int:
    # Evita crash su console Windows con nomi contenenti caratteri non supportati
    sys.stdout.reconfigure(errors="replace")
    args = parse_args()

    load_dotenv(SCRIPT_DIR / ".env")
    api_key = (args.api_key or os.environ.get("RIOT_API_KEY") or API_KEY).strip()
    if not api_key:
        print("API Key mancante. Impostala nel file .env (RIOT_API_KEY=RGAPI-...), "
              "con la variabile d'ambiente RIOT_API_KEY oppure con --api-key.")
        return 1

    riot_id = args.riot_id or input("Riot ID (Nome#TAG): ")
    try:
        game_name, tag_line = parse_riot_id(riot_id)
    except ValueError as exc:
        print(exc)
        return 1

    cache_dir = None if args.no_cache else SCRIPT_DIR / ".cache_matches"
    client = RiotClient(api_key, args.region, cache_dir)

    try:
        print(f"Cerco il PUUID di {game_name}#{tag_line}...")
        puuid = client.get_puuid(game_name, tag_line)

        print(f"Recupero gli ultimi {args.count} match Ranked Solo/Duo...")
        match_ids = client.get_match_ids(puuid, args.count)
        if not match_ids:
            print("Nessuna partita Ranked Solo/Duo trovata (controlla anche --region).")
            return 1
        print(f"Trovati {len(match_ids)} match. Con una Development Key servono circa "
              f"{len(match_ids) / 100 * 2:.0f} minuti (i match gia' in cache sono istantanei).")

        games: list[GameRecord] = []
        for i, match_id in enumerate(match_ids, 1):
            print(f"\r  Scarico match {i}/{len(match_ids)} ({match_id})   ", end="", flush=True)
            try:
                game = extract_game(client.get_match(match_id), puuid)
            except AuthError:
                raise
            except RiotAPIError as exc:
                print(f"\n  [salto {match_id}] {exc}")
                continue
            if game:
                games.append(game)
        print()
    except NotFoundError:
        print(f"Account '{game_name}#{tag_line}' non trovato. Controlla nome e tag.")
        return 1
    except RiotAPIError as exc:
        print(f"Errore API Riot: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nInterrotto dall'utente.")
        return 130

    if not games:
        print("Nessuna partita valida da analizzare (solo remake?).")
        return 1

    partners = detect_duo_partners(games, args.threshold)
    classify_games(games, partners)
    summary = compute_summary(games, partners)
    rows = summary_rows(summary)

    full_riot_id = f"{game_name}#{tag_line}"
    header = build_report_header(full_riot_id, args.region, args.count, len(match_ids),
                                 games, partners, args.threshold)

    # --- Report a terminale ---
    print("\n" + "=" * 70)
    print("  REPORT SOLO QUEUE vs DUO QUEUE")
    print("=" * 70)
    print("\n".join(header))
    print()
    print(render_text_table(rows))
    for label, games_in_group in (("Solo Queue", summary["Solo Queue"]["Partite"]),
                                  ("Duo Queue", summary["Duo Queue"]["Partite"])):
        if games_in_group is not None and games_in_group < 10:
            print(f"\nAttenzione: solo {games_in_group:.0f} partite in {label}, "
                  "il confronto e' poco significativo statisticamente.")

    # --- Salvataggio file ---
    args.output_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^\w\-]+", "_", f"{game_name}_{tag_line}").strip("_")
    md_path = args.output_dir / f"report_{slug}.md"
    summary_csv = args.output_dir / f"summary_{slug}.csv"
    matches_csv = args.output_dir / f"matches_{slug}.csv"

    md_lines = [f"# Solo Queue vs Duo Queue - {full_riot_id}", "",
                f"_Generato il {time.strftime('%Y-%m-%d %H:%M')}_", ""]
    md_lines += [line if line.startswith("  -") else f"{line}  " for line in header]
    md_lines += ["", render_markdown_table(rows), ""]
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    write_summary_csv(summary_csv, summary)
    write_matches_csv(matches_csv, games)

    print(f"\nFile salvati in {args.output_dir}:")
    for path in (md_path, summary_csv, matches_csv):
        print(f"  - {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
