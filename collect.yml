"""
FPL League Tracker — collector

Pulls current standings + full per-team gameweek history for a private
FPL classic league, and upserts into Supabase. Safe to re-run any time —
inserts are deduplicated on (gw, team_id), so nothing doubles up.

Requires three environment variables:
  SUPABASE_URL          e.g. https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY  the service_role key (Project Settings > API)
                         -- NOT the anon key, this needs write access
  LEAGUE_ID             the numeric league ID from the FPL URL

Run: python collector.py
"""

import os
import sys
import requests

FPL_BASE = "https://fantasy.premierleague.com/api"
# FPL's API will 403 requests with no user-agent
HEADERS = {"User-Agent": "Mozilla/5.0 (fpl-league-tracker)"}

# FPL's internal chip names -> short codes, matching the abbreviations already
# used throughout the dashboard (WC/FH/BB/TC)
CHIP_CODES = {
    "wildcard": "WC",
    "freehit": "FH",
    "bboost": "BB",
    "3xc": "TC",
}


def env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return val


def fetch_json(url: str) -> dict:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_league_teams(league_id: str) -> list[dict]:
    """Returns [{team_id, manager_name, team_name}, ...] for every team in the league.

    Before a season starts (or before GW1 has finished), the `standings`
    section of this endpoint is empty — FPL hasn't calculated any ranking
    yet. Anyone who has already joined the league instead shows up under
    `new_entries`. We use standings once they exist, and fall back to
    new_entries so pre-season joiners still get picked up.
    """
    url = f"{FPL_BASE}/leagues-classic/{league_id}/standings/"
    data = fetch_json(url)
    teams = []
    for entry in data["standings"]["results"]:
        teams.append(
            {
                "team_id": entry["entry"],
                "manager_name": entry["player_name"],
                "team_name": entry["entry_name"],
            }
        )

    if not teams:
        print("Standings empty (season likely hasn't started) — using new_entries instead.")
        for entry in data.get("new_entries", {}).get("results", []):
            teams.append(
                {
                    "team_id": entry["entry"],
                    "manager_name": f"{entry['player_first_name']} {entry['player_last_name']}",
                    "team_name": entry["entry_name"],
                }
            )

    # NOTE: standings/ and new_entries/ each only return page 1 by default
    # (usually 50 entries). Fine for any private league, but if
    # has_next is True and yours is bigger than that, you'd need to
    # paginate with ?page_standings=2 / ?page_new_entries=2 etc.
    if data["standings"].get("has_next") or data.get("new_entries", {}).get("has_next"):
        print(
            "Warning: league has more teams than one page returned — "
            "pagination not implemented, some teams may be missing.",
            file=sys.stderr,
        )
    return teams


def fetch_team_history(team_id: int) -> list[dict]:
    """Returns [{gw, gw_points, total_points, chip, transfer_cost, team_value}, ...] for every completed gameweek."""
    url = f"{FPL_BASE}/entry/{team_id}/history/"
    data = fetch_json(url)

    # Chips are returned as a separate list: [{"name": "wildcard", "event": 8}, ...]
    # Build a lookup of which chip (if any) was played each gameweek.
    chip_by_gw = {}
    for chip in data.get("chips", []):
        chip_by_gw[chip["event"]] = CHIP_CODES.get(chip["name"], chip["name"])

    rows = []
    for gw in data["current"]:
        cost = gw.get("event_transfers_cost", 0)
        # FPL reports squad value in tenths of a million (e.g. 1023 -> £102.3m)
        raw_value = gw.get("value")
        team_value = round(raw_value / 10, 1) if raw_value is not None else None
        rows.append(
            {
                "gw": gw["event"],
                "team_id": team_id,
                "gw_points": gw["points"],
                "total_points": gw["total_points"],
                "chip": chip_by_gw.get(gw["event"]),
                "transfer_cost": cost if cost else None,
                "team_value": team_value,
            }
        )
    return rows


def supabase_upsert(table: str, rows: list[dict], on_conflict: str) -> None:
    if not rows:
        return
    url = f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }
    resp = requests.post(url, headers=headers, json=rows, timeout=30)
    if not resp.ok:
        print(f"Supabase upsert to {table} failed: {resp.status_code} {resp.text}", file=sys.stderr)
        resp.raise_for_status()

def main():
    global SUPABASE_URL, SUPABASE_SERVICE_KEY

    SUPABASE_URL = env("SUPABASE_URL").rstrip("/")
    SUPABASE_SERVICE_KEY = env("SUPABASE_SERVICE_KEY")
    league_id = env("LEAGUE_ID")

    print(f"Fetching teams for league {league_id}...")
    teams = fetch_league_teams(league_id)
    print(f"Found {len(teams)} teams.")
    supabase_upsert("teams", teams, on_conflict="team_id")

    all_snapshots = []
    for team in teams:
        print(f"  fetching history for {team['team_name']} ({team['team_id']})...")
        all_snapshots.extend(fetch_team_history(team["team_id"]))

    print(f"Upserting {len(all_snapshots)} gameweek snapshot rows...")
    supabase_upsert("gameweek_snapshots", all_snapshots, on_conflict="gw,team_id")

    print("Done.")


if __name__ == "__main__":
    main()
