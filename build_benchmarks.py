#!/usr/bin/env python3
"""
Benchmark per lega e ruolo
==========================
Costruisce data/benchmarks.json: per ogni lega (Iron ... Master+) e per ogni ruolo, la
distribuzione dei parametri di gioco (KDA, CS/min, danni/min, visione...) usata dalla
dashboard per il radar delle abilita' e i confronti.

Come funziona:
  1. League-V4 fornisce i giocatori delle divisioni III e II, il centro della lega
     (per Master+ la classifica Master).
  2. Si prendono a caso alcuni di questi giocatori e alcune delle loro partite Ranked Solo/Duo recenti.
  3. Ogni partita contribuisce con tutti e 10 i partecipanti: il matchmaking li mette allo
     stesso livello di MMR, quindi rappresentano bene quella lega.

Con 250 partite per lega ogni ruolo ha circa 500 prestazioni di riferimento.

Non serve rieseguirlo ogni giorno: una volta per patch (o al mese) basta. Le partite gia' usate
restano nel campione e in .cache_benchmark/, quindi rieseguirlo scarica solo quelle che mancano.

Gli ID delle partite usate per ogni lega vengono salvati nel file: con --recompute le
distribuzioni si ricalcolano dalla cache, senza chiamate API (utile se cambiano le metriche).

Uso:
    python build_benchmarks.py                       # tutte le leghe, 250 partite ciascuna
    python build_benchmarks.py --tiers GOLD PLATINUM --matches-per-tier 60
    python build_benchmarks.py --recompute           # ricalcola dalle partite gia' scaricate
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from datetime import datetime

from lol_dashboard import BENCHMARK_FILE, load_json, save_json
from lol_duo_analyzer import API_KEY, REGIONS, SCRIPT_DIR, AuthError, RiotAPIError, RiotClient, load_dotenv
from lol_metrics import BENCHMARK_VERSION, build_distribution, outcome_gaps, participant_metrics, team_best_rates

TIERS = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND", "MASTER"]  # MASTER = Master+
PLATFORM_BY_REGION = {"europe": "euw1", "americas": "na1", "asia": "kr", "sea": "oc1"}
BENCHMARK_CACHE = SCRIPT_DIR / ".cache_benchmark"
MATCHES_PER_PLAYER = 3
DEFAULT_MATCHES_PER_TIER = 250
DIVISIONS = ("III", "II")   # il centro della lega: tra la divisione III e la II


def sample_players(client: RiotClient, platform: str, tier: str) -> list[str]:
    """Giocatori della lega in ordine casuale, alternando le divisioni III e II."""
    if tier == "MASTER":
        pools = [client.get_master_league(platform).get("entries", [])]
    else:
        pools = [client.get_league_entries(platform, tier, division) for division in DIVISIONS]
    shuffled = []
    for entries in pools:
        puuids = [e["puuid"] for e in entries if e.get("puuid")]
        random.shuffle(puuids)
        shuffled.append(puuids)
    return [p for group in zip(*shuffled) for p in group] if len(shuffled) > 1 else shuffled[0]


def build_tier(client: RiotClient, platform: str, tier: str, target: int, previous: list[str]) -> dict | None:
    # Le partite gia' nel campione (e in cache) restano: si scaricano solo quelle che mancano
    match_ids = [m for m in previous if (BENCHMARK_CACHE / f"{m}.json").is_file()][:target]
    players = sample_players(client, platform, tier) if len(match_ids) < target else []
    print(f"\n{tier}: {len(match_ids)} partite gia' nel campione, ne cerco altre {max(0, target - len(match_ids))}...")
    for puuid in players:
        if len(match_ids) >= target:
            break
        try:
            ids = client.get_match_ids(puuid, MATCHES_PER_PLAYER)
        except AuthError:
            raise
        except RiotAPIError:
            continue
        match_ids += [m for m in ids if m not in match_ids][: target - len(match_ids)]

    return summarize_tier(tier, [m for m in (fetch(client, mid, i, len(match_ids)) for i, mid in enumerate(match_ids, 1)) if m])


def fetch(client: RiotClient, match_id: str, i: int, total: int) -> dict | None:
    if i % 10 == 0 or i == total:
        print(f"  partite {i}/{total}")
    try:
        return client.get_match(match_id)
    except AuthError:
        raise
    except RiotAPIError as exc:
        print(f"  [salto {match_id}] {exc}")
        return None


def summarize_tier(tier: str, matches: list[dict]) -> dict | None:
    """Distribuzioni per ruolo di una lega a partire dalle sue partite (tutti e 10 i partecipanti)."""
    rows = [m for match in matches for p in match.get("info", {}).get("participants", []) if (m := participant_metrics(match, p))]
    roles = build_distribution(rows)
    if not roles:
        print(f"  {tier}: campione insufficiente, lega saltata.")
        return None
    print(f"  {tier}: {len(matches)} partite, {len(rows)} prestazioni, ruoli: "
          + ", ".join(f"{r} {s['n']}" for r, s in roles.items()))
    return {"version": BENCHMARK_VERSION, "matches": len(matches), "samples": len(rows), "roles": roles,
            "matchIds": [m["metadata"]["matchId"] for m in matches]}


def calibration(data: dict) -> dict | None:
    """Misure ricavate dalle partite di tutte le leghe aggiornate, ognuna con le distribuzioni della sua lega:
    di quanti punti ogni parametro sale nelle vittorie (outcomeGap) e quanto spesso ogni ruolo e' il migliore
    della squadra (teamBest). La dashboard ne ricava le avvertenze e i valori attesi, invece di scriverli a mano."""
    scored, matches = [], []
    tiers = [t for t, v in data.get("tiers", {}).items() if v.get("version") == BENCHMARK_VERSION]
    for tier in tiers:
        roles = data["tiers"][tier]["roles"]
        for mid in data["tiers"][tier].get("matchIds", []):
            match = load_json(BENCHMARK_CACHE / f"{mid}.json", None)
            if not match:
                continue
            matches.append((match, roles))
            for p in match.get("info", {}).get("participants", []):
                if (row := participant_metrics(match, p)) and row["role"] in roles:
                    scored.append((row, roles[row["role"]]))
    if not scored:
        return None
    result = {"version": BENCHMARK_VERSION, "tiers": tiers, "outcomeGap": outcome_gaps(scored), "teamBest": team_best_rates(matches)}
    print("\nScarto vittoria - sconfitta (punti): " + ", ".join(f"{k} {v:+.0f}" for k, v in result["outcomeGap"].items()))
    print("Migliore della squadra: " + ", ".join(f"{r} {v:.0%}" for r, v in result["teamBest"].items()))
    return result


def recompute(data: dict) -> None:
    """Ricalcola le leghe dalle partite in cache (serve che il file contenga gli ID delle partite)."""
    for tier, t in list(data.get("tiers", {}).items()):
        ids = t.get("matchIds")
        if not ids:
            print(f"{tier}: ID delle partite non salvati, serve rieseguire senza --recompute.")
            continue
        matches = [m for mid in ids if (m := load_json(BENCHMARK_CACHE / f"{mid}.json", None))]
        result = summarize_tier(tier, matches)
        if result:
            data["tiers"][tier] = result


def main() -> int:
    sys.stdout.reconfigure(errors="replace")
    load_dotenv(SCRIPT_DIR / ".env")
    parser = argparse.ArgumentParser(description="Costruisce i benchmark per lega e ruolo (data/benchmarks.json).")
    parser.add_argument("--tiers", nargs="+", choices=TIERS, default=TIERS, help="Leghe da calcolare (default: tutte)")
    parser.add_argument("--matches-per-tier", type=int, default=DEFAULT_MATCHES_PER_TIER,
                        help=f"Partite per lega (default: {DEFAULT_MATCHES_PER_TIER})")
    parser.add_argument("--region", choices=REGIONS, default=os.environ.get("RIOT_REGION", "europe"))
    parser.add_argument("--platform", help="Piattaforma per League-V4, es. euw1 (default: dedotta dalla regione)")
    parser.add_argument("--api-key", help="Riot API Key (in alternativa: RIOT_API_KEY nel .env)")
    parser.add_argument("--recompute", action="store_true",
                        help="Ricalcola le distribuzioni dalle partite gia' in cache, senza chiamate API")
    args = parser.parse_args()

    if args.recompute:
        data = load_json(BENCHMARK_FILE, {})
        recompute(data)
        data["calibration"] = calibration(data)
        save_json(BENCHMARK_FILE, data)
        print(f"\nBenchmark ricalcolati in {BENCHMARK_FILE}.")
        return 0

    api_key = (args.api_key or os.environ.get("RIOT_API_KEY") or API_KEY).strip()
    if not api_key:
        print("API Key mancante: impostala nel file .env (RIOT_API_KEY=RGAPI-...).")
        return 1
    platform = args.platform or PLATFORM_BY_REGION[args.region]
    client = RiotClient(api_key, args.region, BENCHMARK_CACHE)

    # Le leghe gia' calcolate e non richieste restano nel file
    data = load_json(BENCHMARK_FILE, {})
    data.setdefault("tiers", {})
    try:
        for tier in args.tiers:
            previous = data["tiers"].get(tier, {}).get("matchIds", [])
            result = build_tier(client, platform, tier, args.matches_per_tier, previous)
            if result:
                data["tiers"][tier] = result
                data.update(generatedAt=datetime.now().isoformat(timespec="seconds"), platform=platform)
                save_json(BENCHMARK_FILE, data)  # salvataggio dopo ogni lega: se si interrompe non si perde nulla
    except AuthError as exc:
        print(f"\n{exc}")
        return 1
    except KeyboardInterrupt:
        print("\nInterrotto: le leghe gia' completate sono salvate.")
        return 130

    data["calibration"] = calibration(data)
    save_json(BENCHMARK_FILE, data)
    stale = [t for t, v in data["tiers"].items() if v.get("version") != BENCHMARK_VERSION]
    if stale:
        print(f"\nLeghe calcolate con formule precedenti, ignorate dalla dashboard: {', '.join(stale)}. "
              "Aggiornale con: python build_benchmarks.py --recompute")
    print(f"\nBenchmark salvati in {BENCHMARK_FILE}. Rigenera le dashboard con: python lol_dashboard.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
