#!/usr/bin/env python3
"""
Benchmark per lega e ruolo
==========================
Costruisce data/benchmarks.json: per ogni lega (Iron ... Master+) e per ogni ruolo, la
distribuzione dei parametri di gioco (KDA, CS/min, danni/min, visione...) usata dalla
dashboard per il radar delle abilita' e i confronti.

Come funziona:
  1. League-V4 fornisce i giocatori di una divisione (si usa la II, il centro della lega;
     per Master+ la classifica Master).
  2. Si prendono a caso alcuni di questi giocatori e una o due delle loro partite Ranked recenti.
  3. Ogni partita contribuisce con tutti e 10 i partecipanti: il matchmaking li mette allo
     stesso livello di MMR, quindi rappresentano bene quella lega.

Non serve rieseguirlo ogni giorno: una volta per patch (o al mese) basta. Le partite scaricate
restano in .cache_benchmark/, quindi rieseguirlo costa solo le partite nuove.

Gli ID delle partite usate per ogni lega vengono salvati nel file: con --recompute le
distribuzioni si ricalcolano dalla cache, senza chiamate API (utile se cambiano le metriche).

Uso:
    python build_benchmarks.py                       # tutte le leghe, 40 partite ciascuna
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
from lol_metrics import build_distribution, participant_metrics

TIERS = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND", "MASTER"]  # MASTER = Master+
PLATFORM_BY_REGION = {"europe": "euw1", "americas": "na1", "asia": "kr", "sea": "oc1"}
BENCHMARK_CACHE = SCRIPT_DIR / ".cache_benchmark"
MATCHES_PER_PLAYER = 2


def sample_players(client: RiotClient, platform: str, tier: str) -> list[str]:
    if tier == "MASTER":
        entries = client.get_master_league(platform).get("entries", [])
    else:
        entries = client.get_league_entries(platform, tier, "II")
    puuids = [e["puuid"] for e in entries if e.get("puuid")]
    random.shuffle(puuids)
    return puuids


def build_tier(client: RiotClient, platform: str, tier: str, target: int) -> dict | None:
    players = sample_players(client, platform, tier)
    print(f"\n{tier}: {len(players)} giocatori in classifica, cerco {target} partite...")
    match_ids: list[str] = []
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
    return {"matches": len(matches), "samples": len(rows), "roles": roles,
            "matchIds": [m["metadata"]["matchId"] for m in matches]}


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
    parser.add_argument("--matches-per-tier", type=int, default=40, help="Partite per lega (default: 40)")
    parser.add_argument("--region", choices=REGIONS, default=os.environ.get("RIOT_REGION", "europe"))
    parser.add_argument("--platform", help="Piattaforma per League-V4, es. euw1 (default: dedotta dalla regione)")
    parser.add_argument("--api-key", help="Riot API Key (in alternativa: RIOT_API_KEY nel .env)")
    parser.add_argument("--recompute", action="store_true",
                        help="Ricalcola le distribuzioni dalle partite gia' in cache, senza chiamate API")
    args = parser.parse_args()

    if args.recompute:
        data = load_json(BENCHMARK_FILE, {})
        recompute(data)
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
            result = build_tier(client, platform, tier, args.matches_per_tier)
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

    print(f"\nBenchmark salvati in {BENCHMARK_FILE}. Rigenera le dashboard con: python lol_dashboard.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
