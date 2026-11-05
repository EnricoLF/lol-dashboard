"""
Metriche per partecipante condivise tra dashboard e benchmark.

Ogni metrica e' calcolata partita per partita con la stessa formula sia per te sia per i
giocatori di riferimento (lobby o campione di una lega), cosi' il confronto e' omogeneo.
Le stesse formule sono replicate in JavaScript in dashboard_template.html (AXES, PING_GROUPS):
se ne cambi una qui, aggiornala anche li'.
"""

from __future__ import annotations

from collections import defaultdict

ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
# Ping: Riot fornisce solo quante volte ogni giocatore ha usato ciascun ping, non quando o dove
PING_GROUPS = {
    "pingWarn": ("enemyMissingPings", "getBackPings", "retreatPings", "dangerPings"),
    "pingHelp": ("assistMePings",),
    "pingCoord": ("onMyWayPings", "allInPings", "pushPings", "holdPings", "commandPings", "basicPings"),
    "pingVision": ("enemyVisionPings", "needVisionPings", "visionClearedPings"),
}
PING_TYPES = tuple(t for types in PING_GROUPS.values() for t in types)
METRIC_KEYS = ("kda", "deaths10", "csm", "cs10", "dpm", "kp", "vspm", "objm", "gpm",
               "pingTotal", *PING_GROUPS)
# Gli 8 parametri del punteggio (radar). Stesse regole in dashboard_template.html (AXES): tenerle allineate.
SCORE_AXES = ("kda", "dpm", "kp", "csm", "cs10", "deaths10", "vspm", "objm")
INVERTED = frozenset({"deaths10"})                 # meno e' meglio: il punteggio e' 100 - percentile
NOT_SCORED = {"UTILITY": frozenset({"csm", "cs10"})}  # i Support forti lasciano i minion all'ADC
MIN_SCORED_AXES = 6                                # sotto questa soglia la partita non ha un punteggio
MEAN_ONLY_KEYS = PING_TYPES  # per i singoli ping bastano media e mediana: niente quantili, file piu' leggero
QUANTILE_STEPS = 20          # quantili ogni 5%: p0, p5, ..., p100 (21 valori)
MIN_ROLE_SAMPLES = 15        # sotto questa soglia un ruolo non ha un riferimento affidabile
REMAKE_MAX_SECONDS = 300


def match_duration_minutes(info: dict) -> float:
    # Dalla patch 11.20 gameDuration e' in secondi; prima (senza gameEndTimestamp) in millisecondi
    seconds = info.get("gameDuration", 0)
    if "gameEndTimestamp" not in info:
        seconds /= 1000
    return seconds / 60


def cs_at_10(participant: dict) -> int | None:
    """CS (minion di corsia + mostri della giungla) nei primi 10 minuti, o None se mancano le challenges."""
    challenges = participant.get("challenges") or {}
    if "laneMinionsFirst10Minutes" not in challenges:
        return None
    return round(challenges.get("laneMinionsFirst10Minutes", 0) + challenges.get("jungleCsBefore10Minutes", 0))


def ping_counts(participant: dict) -> dict:
    """Ping per categoria e totali in una partita (None se la partita non riporta i ping)."""
    if "assistMePings" not in participant:
        return {key: None for key in ("pingTotal", *PING_GROUPS, *PING_TYPES)}
    counts = {t: participant.get(t, 0) for t in PING_TYPES}
    groups = {group: sum(counts[t] for t in types) for group, types in PING_GROUPS.items()}
    return {"pingTotal": sum(counts.values()), **groups, **counts}


def participant_metrics(match: dict, participant: dict) -> dict | None:
    """Metriche di un partecipante in una partita, o None per remake / ruolo sconosciuto."""
    info = match.get("info", {})
    minutes = match_duration_minutes(info)
    role = participant.get("teamPosition")
    if minutes * 60 < REMAKE_MAX_SECONDS or participant.get("gameEndedInEarlySurrender") or role not in ROLES:
        return None

    team_kills = sum(p.get("kills", 0) for p in info.get("participants", [])
                     if p.get("teamId") == participant.get("teamId"))
    kills, deaths, assists = participant.get("kills", 0), participant.get("deaths", 0), participant.get("assists", 0)
    return {
        "role": role,
        "kda": (kills + assists) / max(1, deaths),
        "deaths10": deaths / (minutes / 10),
        "csm": (participant.get("totalMinionsKilled", 0) + participant.get("neutralMinionsKilled", 0)) / minutes,
        "cs10": cs_at_10(participant),
        "dpm": participant.get("totalDamageDealtToChampions", 0) / minutes,
        "kp": (kills + assists) / team_kills if team_kills else 0.0,
        "vspm": participant.get("visionScore", 0) / minutes,
        "objm": participant.get("damageDealtToObjectives", 0) / minutes,
        "gpm": participant.get("goldEarned", 0) / minutes,
        **ping_counts(participant),
    }


def quantiles(values: list[float]) -> list[float]:
    """Quantili ogni 5% con interpolazione lineare (stesso metodo di numpy 'linear')."""
    ordered = sorted(values)
    last = len(ordered) - 1
    result = []
    for step in range(QUANTILE_STEPS + 1):
        pos = last * step / QUANTILE_STEPS
        low = int(pos)
        high = min(low + 1, last)
        result.append(ordered[low] + (ordered[high] - ordered[low]) * (pos - low))
    return result


def percentile_of(value: float, q: list[float]) -> float:
    """Percentile (0-100) di value in una distribuzione descritta dai quantili ogni 5%, interpolando.

    Identica a percentileOf() in dashboard_template.html: i punteggi calcolati qui e nella pagina coincidono.
    """
    n = len(q) - 1
    step = 100 / n
    if value < q[0]:
        return 0.0
    if value > q[n]:
        return 100.0
    equal = [i for i, x in enumerate(q) if x == value]
    if equal:                       # valori ripetuti (es. 0 morti): centro dell'intervallo
        return (equal[0] + equal[-1]) / 2 * step
    i = 0
    while q[i + 1] < value:
        i += 1
    return (i + (value - q[i]) / (q[i + 1] - q[i])) * step


def axis_score(key: str, value: float | None, role_stats: dict) -> float | None:
    """Punteggio 0-100 di un parametro: quante prestazioni di riferimento (stesso ruolo) hai superato."""
    stats = role_stats.get(key)
    if value is None or not stats or "q" not in stats:
        return None
    p = percentile_of(value, stats["q"])
    return 100 - p if key in INVERTED else p


def scored_axes(role: str) -> tuple[str, ...]:
    return tuple(k for k in SCORE_AXES if k not in NOT_SCORED.get(role, ()))


def raw_game_score(row: dict, role_stats: dict) -> float | None:
    """Media dei punteggi dei parametri in una partita (prima della calibrazione)."""
    scores = [s for k in scored_axes(row["role"]) if (s := axis_score(k, row.get(k), role_stats)) is not None]
    return sum(scores) / len(scores) if len(scores) >= MIN_SCORED_AXES else None


def build_distribution(rows: list[dict]) -> dict:
    """{ruolo: {metrica: {"q": quantili, "mean": media}, "n": campioni}} dalle righe di participant_metrics.

    "score" e' la distribuzione del punteggio grezzo delle partite di riferimento: serve a calibrare il
    punteggio partita, che cosi' diventa anch'esso un percentile (50 = partita tipica di quella lega).
    """
    by_role: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_role[row["role"]].append(row)

    result = {}
    for role, role_rows in by_role.items():
        if len(role_rows) < MIN_ROLE_SAMPLES:
            continue
        stats = {"n": len(role_rows)}
        for key in METRIC_KEYS:
            values = [r[key] for r in role_rows if r[key] is not None]  # cs10 manca nei match senza challenges
            if len(values) < MIN_ROLE_SAMPLES:
                continue
            stats[key] = {"q": [round(v, 4) for v in quantiles(values)], "mean": round(sum(values) / len(values), 4)}
        for key in MEAN_ONLY_KEYS:
            values = [r[key] for r in role_rows if r.get(key) is not None]
            if len(values) >= MIN_ROLE_SAMPLES:
                # la mediana non risente delle partite con ping a raffica (spam)
                stats[key] = {"mean": round(sum(values) / len(values), 4), "median": quantiles(values)[QUANTILE_STEPS // 2]}
        raw = [s for row in role_rows if (s := raw_game_score(row, stats)) is not None]
        if len(raw) >= MIN_ROLE_SAMPLES:
            stats["score"] = {"q": [round(v, 4) for v in quantiles(raw)], "mean": round(sum(raw) / len(raw), 4)}
        result[role] = stats
    return result
