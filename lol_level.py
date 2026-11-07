"""
Livello stimato di un account: la lega piu' vicina alle sue prestazioni.

Idea: per ogni lega si calcola il punteggio (0-100) che avresti se le tue partite fossero confrontate
con i giocatori di quella lega, nello stesso ruolo. 50 vuol dire "giocatore tipico di quella lega".
Il punteggio scende salendo di lega; la lega piu' vicina e' quella in cui scende a 50.

Si usano solo le partite nel ruolo principale (quello giocato di piu') e solo i parametri che cambiano
davvero da una lega all'altra in quel ruolo (es. CS/min). KDA, morti e danni restano quasi uguali da
Iron a Master, perche' ogni uccisione e' la morte di qualcun altro: confrontarli con un'altra lega non
dice nulla sul livello.
"""

from __future__ import annotations

import math
import random
from collections import Counter

from lol_metrics import ROLES, INVERTED, game_score, row_axis_score, scored_axes

TIER_ORDER = ("IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND", "MASTER")
TIER_NAMES = {"IRON": "Iron", "BRONZE": "Bronze", "SILVER": "Silver", "GOLD": "Gold", "PLATINUM": "Platinum",
              "EMERALD": "Emerald", "DIAMOND": "Diamond", "MASTER": "Master+"}
DIVISIONS = ("IV", "III", "II", "I")
MIN_SEPARATION = 0.4   # un parametro "distingue le leghe" se da Iron a Master si sposta di almeno 0,4 IQR
MIN_GAMES = 20
BOOTSTRAP = 300        # ricampionamenti per l'intervallo di incertezza (seme fisso: il risultato non oscilla)
INTERVAL = 0.8         # intervallo che contiene la stima con probabilita' 80%


def separation(tiers: dict, role: str, key: str) -> float | None:
    """Di quanto il valore tipico (mediana) migliora da Iron a Master, in unita' di dispersione (IQR).

    Pendenza della retta che passa per le mediane delle 8 leghe, moltiplicata per 7 (da Iron a Master).
    Positiva se nelle leghe alte il parametro e' migliore (per le morti: piu' basso).
    """
    medians, spreads = [], []
    for tier in TIER_ORDER:
        stats = tiers.get(tier, {}).get("roles", {}).get(role, {}).get(key)
        if not stats or "q" not in stats:
            return None
        medians.append(stats["q"][10])
        spreads.append(stats["q"][15] - stats["q"][5])
    spread = sum(spreads) / len(spreads)
    if not spread:
        return None
    x_mean = (len(medians) - 1) / 2
    y_mean = sum(medians) / len(medians)
    slope = (sum((i - x_mean) * (m - y_mean) for i, m in enumerate(medians))
             / sum((i - x_mean) ** 2 for i in range(len(medians))))
    sep = slope * (len(medians) - 1) / spread
    return -sep if key in INVERTED else sep


def level_axes(tiers: dict, role: str) -> tuple[str, ...]:
    """Parametri che distinguono le leghe in quel ruolo."""
    return tuple(k for k in scored_axes(role) if (s := separation(tiers, role, k)) is not None and s >= MIN_SEPARATION)


def decreasing(values: list[float]) -> list[float]:
    """Regressione isotonica decrescente (pool adjacent violators): toglie le piccole oscillazioni dovute
    al campione, cosi' il punteggio non risale passando a una lega piu' alta."""
    blocks = [[v, 1] for v in values]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] < blocks[i + 1][0]:
            total = blocks[i][0] * blocks[i][1] + blocks[i + 1][0] * blocks[i + 1][1]
            count = blocks[i][1] + blocks[i + 1][1]
            blocks[i:i + 2] = [[total / count, count]]
            i = max(i - 1, 0)
        else:
            i += 1
    return [v for v, count in blocks for _ in range(count)]


def crossing(curve: list[float]) -> tuple[float, str | None]:
    """Posizione (0 = Iron ... 7 = Master+) in cui il punteggio scende a 50.

    Restituisce anche "min" se sei sotto la media gia' in Iron, "max" se sei sopra la media anche in Master+.
    """
    y = decreasing(curve)
    if y[0] < 50:
        return 0.0, "min"
    if y[-1] >= 50:
        return float(len(y) - 1), "max"
    for i in range(len(y) - 1):
        if y[i] >= 50 > y[i + 1]:
            return i + (y[i] - 50) / (y[i] - y[i + 1]), None
    return float(len(y) - 1), "max"


def position_label(position: float) -> dict:
    """Lega piu' vicina e fascia: ogni lega e' campionata al suo centro (divisioni III-II)."""
    index = min(len(TIER_ORDER) - 1, max(0, round(position)))
    offset = position - index
    band = "alto" if offset >= 0.17 else "basso" if offset <= -0.17 else ""
    tier = TIER_ORDER[index]
    if tier == "MASTER":
        band = ""   # Master+ non ha divisioni: niente fascia
    return {"tier": tier, "band": band, "label": f"{TIER_NAMES[tier]} {band}".strip()}


def rank_position(rank: dict | None) -> float | None:
    """Rank attuale sulla stessa scala: Gold II 0 LP (centro della lega) = 3.0, ogni lega vale 1."""
    if not rank or rank.get("tier") not in (*TIER_ORDER, "GRANDMASTER", "CHALLENGER"):
        return None
    if rank["tier"] in ("MASTER", "GRANDMASTER", "CHALLENGER"):
        ladder = 7 * 400 + rank.get("lp", 0)
    elif rank.get("rank") in DIVISIONS:
        ladder = TIER_ORDER.index(rank["tier"]) * 400 + DIVISIONS.index(rank["rank"]) * 100 + rank.get("lp", 0)
    else:
        return None
    return (ladder - 200) / 400


def estimate_level(all_rows: list[dict], tiers: dict, rank: dict | None, lobby: dict | None) -> dict | None:
    """Livello stimato nel ruolo principale, dalle righe di participant_metrics delle tue partite
    (gia' filtrate: es. stagione).

    Si usa solo il ruolo che giochi di piu': mescolare i ruoli abbasserebbe la stima di chi gioca bene il
    suo ruolo e peggio quelli secondari. all_rows: righe con "role" e i valori dei parametri.
    tiers: benchmarks.json["tiers"]. lobby: distribuzioni della tua lobby (i 9 giocatori delle tue
    partite), per il punteggio rispetto al tuo MMR.
    """
    available = [t for t in TIER_ORDER if t in tiers]
    if len(available) < len(TIER_ORDER) or not all_rows:
        return None
    main_role = Counter(r["role"] for r in all_rows).most_common(1)[0][0]
    rows = [r for r in all_rows if r["role"] == main_role]
    if len(rows) < MIN_GAMES:
        return None
    # Parametri che distinguono le leghe, per ogni ruolo (dipendono solo dalle medie di lega)
    axes_by_role = {role: level_axes(tiers, role) for role in ROLES}

    # Per ogni partita e ogni lega: media dei punteggi nei parametri che distinguono le leghe
    matrix = []
    for row in rows:
        keys = axes_by_role[row["role"]]
        line = []
        for tier in TIER_ORDER:
            role_stats = tiers[tier]["roles"].get(row["role"], {})
            scores = [s for k in keys if (s := row_axis_score(k, row, role_stats)) is not None]
            line.append(sum(scores) / len(scores) if scores else None)
        if None not in line:
            matrix.append(line)
    if len(matrix) < MIN_GAMES:
        return None

    def curve_of(lines: list[list[float]]) -> list[float]:
        return [sum(line[t] for line in lines) / len(lines) for t in range(len(TIER_ORDER))]

    curve = curve_of(matrix)
    position, bound = crossing(curve)
    rng = random.Random(2026)
    samples = sorted(crossing(curve_of([rng.choice(matrix) for _ in matrix]))[0] for _ in range(BOOTSTRAP))
    tail = (1 - INTERVAL) / 2
    low, high = samples[int(tail * BOOTSTRAP)], samples[int((1 - tail) * BOOTSTRAP) - 1]

    # Seconda misura: quanti compagni di squadra superi (tutti gli 8 parametri). Tra compagni il risultato
    # e' lo stesso, quindi non dipende da vittoria o sconfitta; 50% = giochi come i giocatori del tuo MMR.
    team = [r for r in rows if r.get("team") is not None]
    team_info = None
    if len(team) >= MIN_GAMES:
        beats = [r["team"] for r in team]
        rng_team = random.Random(2027)
        boot = sorted(sum(rng_team.choice(beats) for _ in beats) / len(beats) for _ in range(BOOTSTRAP))
        team_low, team_high = boot[int(tail * BOOTSTRAP)], boot[int((1 - tail) * BOOTSTRAP) - 1]
        team_info = {
            "teamGames": len(team),
            "teamScore": round(sum(beats) / len(beats) * 100, 1),
            "teamLow": round(team_low * 100, 1),
            "teamHigh": round(team_high * 100, 1),
            "teamRank": round(sum(r["teamRank"] for r in team) / len(team), 2),
            "teamBest": round(sum(r["teamRank"] == 1 for r in team) / len(team), 3),
            "teamSignal": 1 if team_low > 0.5 else -1 if team_high < 0.5 else 0,
        }

    # Risultati reali nel ruolo principale: il rank segue le vittorie, quindi la pagina li mostra accanto al
    # giudizio sulle statistiche (che non e' una previsione) e dice quando vanno in direzione opposta.
    wins = sum(r["win"] for r in rows)
    wr_low, wr_high = wilson(wins, len(rows))
    results = {"wins": wins, "losses": len(rows) - wins, "winRate": round(wins / len(rows), 3),
               "wrLow": round(wr_low, 3), "wrHigh": round(wr_high, 3),
               "resultSignal": 1 if wr_low > 0.5 else -1 if wr_high < 0.5 else 0}

    # Giudizio sulle statistiche: le due misure si sommano. Farm sopra il rank = +1, sotto = -1; superi piu'
    # (o meno) della meta' dei compagni in modo netto (tutto l'intervallo 80%) = +1 (o -1). Totale > 0 sopra,
    # < 0 sotto. Descrive come giochi, non dice dove andrai: per quello contano i risultati.
    current = rank_position(rank)
    farm_signal = None
    if current is not None:
        farm_signal = 1 if low > current + 0.25 else -1 if high < current - 0.25 else 0
    if farm_signal is None:
        verdict = None
    else:
        total = farm_signal + (team_info["teamSignal"] if team_info else 0)
        verdict = "above" if total > 0 else "below" if total < 0 else "inline"

    # Livello di ogni parametro che distingue le leghe nel ruolo principale
    axis_levels = {}
    for key in axes_by_role[main_role]:
        lines = [line for row in rows
                 if None not in (line := [row_axis_score(key, row, tiers[t]["roles"].get(main_role, {})) for t in TIER_ORDER])]
        if len(lines) >= MIN_GAMES // 2:
            pos, axis_bound = crossing(curve_of(lines))
            axis_levels[key] = {"position": round(pos, 2), "bound": axis_bound, "games": len(lines), **position_label(pos)}

    own_tier = None
    if rank and rank.get("tier") in TIER_ORDER:
        own_tier = rank["tier"]
    elif rank and rank.get("tier") in ("GRANDMASTER", "CHALLENGER"):
        own_tier = "MASTER"

    return {
        "role": main_role,
        "games": len(matrix),
        "totalGames": len(all_rows),
        "position": round(position, 2),
        "low": round(low, 2),
        "high": round(high, 2),
        "bound": bound,
        **position_label(position),
        "lowLabel": position_label(low)["label"],
        "highLabel": position_label(high)["label"],
        "rankPosition": None if current is None else round(current, 2),
        "gap": None if current is None else round(position - current, 2),
        "verdict": verdict,
        "farmSignal": farm_signal,
        **(team_info or {}),
        **results,
        "curve": {t: round(v, 1) for t, v in zip(TIER_ORDER, curve)},
        "axesByRole": {role: list(keys) for role, keys in axes_by_role.items()},
        "axisLevels": axis_levels,
        "ownTier": own_tier,
        "scoreOwnTier": overall_score(rows, tiers[own_tier]["roles"]) if own_tier else None,
        "scoreLobby": overall_score(rows, lobby) if lobby else None,
    }


def overall_score(rows: list[dict], roles: dict) -> float | None:
    """Media dei punteggi partita calibrati (tutti gli 8 parametri, a parita' di risultato) rispetto a un riferimento."""
    scores = [s for row in rows if (s := game_score(row, roles)) is not None]
    return round(sum(scores) / len(scores), 1) if len(scores) >= MIN_GAMES else None


def wilson(successes: int, total: int, level: float = INTERVAL) -> tuple[float, float]:
    """Intervallo di Wilson per una proporzione (es. win rate): contiene il valore vero con probabilita' `level`."""
    z = {0.8: 1.2816, 0.9: 1.6449, 0.95: 1.96}[level]
    p = successes / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return centre - half, centre + half
