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
METRIC_KEYS = ("kda", "deaths10", "csm", "cs10", "dpm", "hspm", "mitpm", "kp", "vspm", "objp", "objm", "gpm",
               "pingTotal", *PING_GROUPS)
# Gli 8 parametri del punteggio (radar), uguali per tutti i ruoli. Stesse regole in dashboard_template.html
# (AXES, impactRaw): tenerle allineate.
SCORE_AXES = ("kda", "impm", "kp", "csm", "cs10", "deaths10", "vspm", "objp")
# Impatto in combattimento: il migliore dei contributi, ognuno come percentile nel ruolo. Cosi' un carry conta
# per i danni, un tank per i danni assorbiti e, da Support, un incantatore per cure e scudi. Negli altri ruoli
# cure e scudi ai compagni restano fuori: valgono 0 in circa 9 partite su 10, quindi il loro percentile non
# distingue nulla (0 cadeva a meta' del blocco di zeri, ~45, e una cura minima valeva gia' oltre 85).
IMPACT_PARTS = ("dpm", "hspm", "mitpm")
IMPACT_PARTS_BY_ROLE = {"UTILITY": IMPACT_PARTS}
DEFAULT_IMPACT_PARTS = ("dpm", "mitpm")
INVERTED = frozenset({"deaths10"})                 # meno e' meglio: il punteggio e' 100 - percentile
NOT_SCORED = {"UTILITY": frozenset({"csm", "cs10"})}  # i Support forti lasciano i minion all'ADC
MAX_MISSING_AXES = 2   # una partita ha un punteggio se mancano al massimo 2 dei suoi parametri
MEAN_ONLY_KEYS = PING_TYPES  # per i singoli ping bastano media e mediana: niente quantili, file piu' leggero
QUANTILE_STEPS = 20          # quantili ogni 5%: p0, p5, ..., p100 (21 valori)
MIN_ROLE_SAMPLES = 15        # sotto questa soglia un ruolo non ha un riferimento affidabile
# Sotto i 10 minuti (es. resa per un giocatore assente) una partita non si valuta: i valori al minuto non sono
# confrontabili e i CS a 10 minuti non esistono. Resta nel win rate. Stessa soglia in dashboard_template.html.
MIN_SCORED_MINUTES = 10
# Versione delle formule usate per le distribuzioni di benchmarks.json: una lega calcolata con formule
# diverse non si confronta con le partite di oggi (rigenerarla con build_benchmarks.py --recompute).
BENCHMARK_VERSION = 2
# Parametri di cui si misura quanto dipendono dal risultato (scarto vittoria - sconfitta nei benchmark)
GAP_KEYS = (*SCORE_AXES, "gpm")


def match_duration_minutes(info: dict) -> float:
    # Dalla patch 11.20 gameDuration e' in secondi; prima (senza gameEndTimestamp) in millisecondi
    seconds = info.get("gameDuration", 0)
    if "gameEndTimestamp" not in info:
        seconds /= 1000
    return seconds / 60


def cs_at_10(participant: dict, minutes: float) -> int | None:
    """CS (minion di corsia + mostri della giungla) nei primi 10 minuti, o None se mancano le challenges o la
    partita e' finita prima dei 10 minuti (Riot conta i CS fino alla fine, che qui arriva prima)."""
    challenges = participant.get("challenges") or {}
    if "laneMinionsFirst10Minutes" not in challenges or minutes < MIN_SCORED_MINUTES:
        return None
    return round(challenges.get("laneMinionsFirst10Minutes", 0) + challenges.get("jungleCsBefore10Minutes", 0))


def objective_share(info: dict, participant: dict) -> float | None:
    """Quota di torri, draghi e Baroni della tua squadra a cui hai partecipato (uccisione o assist).

    I Voidgrubs restano fuori: il conteggio di Riot per giocatore non torna con il totale della squadra.
    """
    team = next((t for t in info.get("teams", []) if t.get("teamId") == participant.get("teamId")), None)
    if team is None:
        return None
    objectives = team.get("objectives", {})
    total = sum(objectives.get(k, {}).get("kills", 0) for k in ("tower", "dragon", "baron"))
    if not total:
        return None
    challenges = participant.get("challenges") or {}
    mine = participant.get("turretTakedowns", 0) + challenges.get("dragonTakedowns", 0) + challenges.get("baronTakedowns", 0)
    return min(1.0, mine / total)


def ally_protection(participant: dict) -> int:
    """Cure e scudi dati ai compagni."""
    return participant.get("totalHealsOnTeammates", 0) + participant.get("totalDamageShieldedOnTeammates", 0)


def ping_counts(participant: dict) -> dict:
    """Ping per categoria e totali in una partita (None se la partita non riporta i ping)."""
    if "assistMePings" not in participant:
        return {key: None for key in ("pingTotal", *PING_GROUPS, *PING_TYPES)}
    counts = {t: participant.get(t, 0) for t in PING_TYPES}
    groups = {group: sum(counts[t] for t in types) for group, types in PING_GROUPS.items()}
    return {"pingTotal": sum(counts.values()), **groups, **counts}


def participant_metrics(match: dict, participant: dict) -> dict | None:
    """Metriche di un partecipante in una partita, o None per remake, partite sotto i 10 minuti, ruolo sconosciuto."""
    info = match.get("info", {})
    minutes = match_duration_minutes(info)
    role = participant.get("teamPosition")
    if minutes < MIN_SCORED_MINUTES or participant.get("gameEndedInEarlySurrender") or role not in ROLES:
        return None

    team_kills = sum(p.get("kills", 0) for p in info.get("participants", [])
                     if p.get("teamId") == participant.get("teamId"))
    kills, deaths, assists = participant.get("kills", 0), participant.get("deaths", 0), participant.get("assists", 0)
    return {
        "role": role,
        "win": bool(participant.get("win")),
        "kda": (kills + assists) / max(1, deaths),
        "deaths10": deaths / (minutes / 10),
        "csm": (participant.get("totalMinionsKilled", 0) + participant.get("neutralMinionsKilled", 0)) / minutes,
        "cs10": cs_at_10(participant, minutes),
        "dpm": participant.get("totalDamageDealtToChampions", 0) / minutes,
        "hspm": ally_protection(participant) / minutes,
        "mitpm": participant.get("damageSelfMitigated", 0) / minutes,
        "kp": (kills + assists) / team_kills if team_kills else None,   # senza uccisioni di squadra non e' definita
        "vspm": participant.get("visionScore", 0) / minutes,
        "objp": objective_share(info, participant),
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


def impact_parts(role: str) -> tuple[str, ...]:
    """Contributi dell'indice di impatto nel ruolo: cure e scudi ai compagni solo per i Support."""
    return IMPACT_PARTS_BY_ROLE.get(role, DEFAULT_IMPACT_PARTS)


def impact_raw(row: dict, role_stats: dict) -> float | None:
    """Indice di impatto (0-100): il migliore dei contributi del ruolo, come percentile nel ruolo.

    Un contributo nullo vale 0, non il centro del blocco di partite a zero: non aver dato cure o scudi non
    deve valere come un contributo nella media. Identica a impactRaw() in dashboard_template.html.
    """
    parts = [0.0 if row[k] <= 0 else percentile_of(row[k], role_stats[k]["q"]) for k in impact_parts(row["role"])
             if row.get(k) is not None and "q" in role_stats.get(k, {})]
    return max(parts) if parts else None


def axis_value(key: str, row: dict, role_stats: dict) -> float | None:
    """Il valore da confrontare per un parametro: per l'impatto e' l'indice, per gli altri il valore grezzo."""
    return impact_raw(row, role_stats) if key == "impm" else row.get(key)


def axis_score(key: str, value: float | None, role_stats: dict) -> float | None:
    """Punteggio 0-100 di un parametro: quante prestazioni di riferimento (stesso ruolo) hai superato.

    Per l'impatto value e' gia' l'indice (axis_value): lo si confronta con la distribuzione dell'indice.
    """
    stats = role_stats.get(key)
    if value is None or not stats or "q" not in stats:
        return None
    p = percentile_of(value, stats["q"])
    return 100 - p if key in INVERTED else p


def row_axis_score(key: str, row: dict, role_stats: dict) -> float | None:
    return axis_score(key, axis_value(key, row, role_stats), role_stats)


def scored_axes(role: str) -> tuple[str, ...]:
    return tuple(k for k in SCORE_AXES if k not in NOT_SCORED.get(role, ()))


def min_scored_axes(role: str) -> int:
    """Parametri minimi per un punteggio partita: 6 su 8, per i Support 4 su 6 (es. nessun obiettivo preso)."""
    return len(scored_axes(role)) - MAX_MISSING_AXES


def raw_game_score(row: dict, role_stats: dict) -> float | None:
    """Media dei punteggi dei parametri in una partita (prima della calibrazione)."""
    scores = [s for k in scored_axes(row["role"]) if (s := row_axis_score(k, row, role_stats)) is not None]
    return sum(scores) / len(scores) if len(scores) >= min_scored_axes(row["role"]) else None


def calibrate(raw: float, win: bool, role_stats: dict) -> float:
    """Punteggio grezzo -> percentile tra le partite del riferimento con lo stesso risultato.

    Una vittoria si confronta con le vittorie, una sconfitta con le sconfitte: chi vince ha quasi sempre KDA e
    morti migliori, e senza questa regola il punteggio direbbe soprattutto se la partita e' stata vinta.
    Se il riferimento non ha abbastanza vittorie e sconfitte nel ruolo si usano tutte le sue partite.
    Identica a calibrate() in dashboard_template.html.
    """
    if "scoreWin" in role_stats and "scoreLoss" in role_stats:
        return percentile_of(raw, role_stats["scoreWin" if win else "scoreLoss"]["q"])
    return percentile_of(raw, role_stats["score"]["q"]) if "score" in role_stats else raw


def game_score(row: dict | None, roles: dict) -> float | None:
    """Punteggio partita calibrato (0-100) rispetto a un riferimento {ruolo: distribuzioni}: 50 = partita
    tipica del riferimento, nello stesso ruolo e con lo stesso risultato."""
    if not row:
        return None
    role_stats = roles.get(row["role"], {})
    raw = raw_game_score(row, role_stats)
    return None if raw is None else calibrate(raw, row["win"], role_stats)


def team_positions(matches: list[dict], puuid: str, roles: dict) -> dict[str, dict]:
    """Posizione in squadra e in partita, come l'OP Score: {matchId: {"rank", "beat", "rank10", "badge"}}.

    Tutti e 10 i giocatori vengono ordinati per punteggio partita (tutti i parametri, ognuno confrontato
    con il proprio ruolo). rank: posizione tra i 5 compagni (1 = il migliore). Tra compagni il risultato
    e' lo stesso, quindi vittoria o sconfitta non falsano il confronto. beat: quota di compagni superati
    (i pareggi valgono mezzo). rank10: posizione tra i 10 giocatori. badge: "MVP" se sei il migliore della
    squadra che ha vinto, "ACE" se sei il migliore di quella che ha perso.
    """
    result = {}
    for match in matches:
        participants = match.get("info", {}).get("participants", [])
        me = next((p for p in participants if p.get("puuid") == puuid), None)
        if me is None:
            continue
        scores = {p.get("puuid"): (p, game_score(participant_metrics(match, p), roles)) for p in participants}
        team = {k: s for k, (p, s) in scores.items() if p.get("teamId") == me.get("teamId")}
        mine = team.get(puuid)
        if len(team) != 5 or None in team.values():
            continue   # remake, ruoli mancanti o parametri insufficienti
        others = [s for k, s in team.items() if k != puuid]
        higher = sum(s > mine for s in others)
        ties = sum(s == mine for s in others)
        entry = {"rank": 1 + higher, "beat": (4 - higher - ties / 2) / 4}
        everyone = [s for _, s in scores.values()]
        if len(everyone) == 10 and None not in everyone:
            entry["rank10"] = 1 + sum(s > mine for s in everyone)
            if higher == 0:
                entry["badge"] = "MVP" if me.get("win") else "ACE"
        result[match["metadata"]["matchId"]] = entry
    return result


def build_distribution(rows: list[dict]) -> dict:
    """{ruolo: {metrica: {"q": quantili, "mean": media}, "n": campioni}} dalle righe di participant_metrics.

    "score" e' la distribuzione del punteggio grezzo delle partite di riferimento, "scoreWin" e "scoreLoss" la
    stessa per le sole vittorie e le sole sconfitte: servono a calibrare il punteggio partita, che cosi' diventa
    anch'esso un percentile (50 = partita tipica di quella lega con lo stesso risultato).
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
        # Indice di impatto: serve la distribuzione dei suoi tre contributi, quindi si calcola dopo
        impact = [v for row in role_rows if (v := impact_raw(row, stats)) is not None]
        if len(impact) >= MIN_ROLE_SAMPLES:
            stats["impm"] = {"q": [round(v, 4) for v in quantiles(impact)], "mean": round(sum(impact) / len(impact), 4)}
        raw = [(s, row["win"]) for row in role_rows if (s := raw_game_score(row, stats)) is not None]
        for key, values in (("score", [s for s, _ in raw]), ("scoreWin", [s for s, win in raw if win]),
                            ("scoreLoss", [s for s, win in raw if not win])):
            if len(values) >= MIN_ROLE_SAMPLES:
                stats[key] = {"q": [round(v, 4) for v in quantiles(values)], "mean": round(sum(values) / len(values), 4)}
        result[role] = stats
    return result


def outcome_gaps(scored: list[tuple[dict, dict]]) -> dict[str, float]:
    """Di quanti punti (0-100) sale ogni parametro nelle vittorie rispetto alle sconfitte.

    scored: coppie (riga di participant_metrics, distribuzioni del suo ruolo), es. tutte le prestazioni dei
    benchmark ognuna con la sua lega. E' la misura di quanto un parametro sia una conseguenza del risultato.
    """
    sums = {key: [[], []] for key in GAP_KEYS}
    for row, role_stats in scored:
        for key in GAP_KEYS:
            if key in SCORE_AXES and key not in scored_axes(row["role"]):
                continue
            if (s := row_axis_score(key, row, role_stats)) is not None:
                sums[key][0 if row["win"] else 1].append(s)
    return {key: round(sum(w) / len(w) - sum(l) / len(l), 1) for key, (w, l) in sums.items() if w and l}


def team_best_rates(matches: list[tuple[dict, dict]]) -> dict[str, float]:
    """Quota di partite in cui ogni ruolo e' il migliore della sua squadra per punteggio partita.

    matches: coppie (partita, riferimento {ruolo: distribuzioni}). Con un punteggio equo varrebbe 20% per tutti;
    serve come valore atteso quando si guarda quante volte sei stato il migliore della squadra.
    """
    best, seen = defaultdict(int), defaultdict(int)
    for match, roles in matches:
        players = [(p, game_score(participant_metrics(match, p), roles)) for p in match.get("info", {}).get("participants", [])]
        for team_id in {p.get("teamId") for p, _ in players}:
            team = [(p, s) for p, s in players if p.get("teamId") == team_id]
            if len(team) != 5 or any(s is None for _, s in team):
                continue
            top = max(s for _, s in team)
            for p, s in team:
                seen[p["teamPosition"]] += 1
                best[p["teamPosition"]] += s == top
    return {role: round(best[role] / seen[role], 3) for role in ROLES if seen[role]}
