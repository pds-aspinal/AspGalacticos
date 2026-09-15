"""
FPL League Tracker — collector

Pulls current standings + full per-team gameweek history for a private
FPL classic league, and upserts into Supabase. Safe to re-run any time —
inserts are deduplicated on (gw, team_id), so nothing doubles up.

Also tracks:
  - Player price changes (by diffing today's prices against the last
    known state stored in the `players` table)
  - Full transfer history per manager (FPL returns each manager's
    complete transfer log every call, so this backfills retroactively
    on first run, then just skips duplicates)

Requires three environment variables:
  SUPABASE_URL          e.g. https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY  the service_role key (Project Settings > API)
                         -- NOT the anon key, this needs write access
  LEAGUE_ID             the numeric league ID from the FPL URL

Run: python collector.py
"""

import os
import sys
import datetime
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

# FPL's element_type ids -> position codes
POSITION_CODES = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}


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


def fetch_all_players() -> list[dict]:
    """Returns [{player_id, web_name, team_short, position, now_cost}, ...] for every player in the game.

    Pulled from bootstrap-static, which is FPL's single "everything about
    the current state of the game" endpoint. Prices here are in tenths of
    a million (e.g. 55 -> £5.5m), same convention as team_value above.
    """
    data = fetch_json(f"{FPL_BASE}/bootstrap-static/")
    team_short_by_id = {t["id"]: t["short_name"] for t in data["teams"]}

    players = []
    for p in data["elements"]:
        players.append(
            {
                "player_id": p["id"],
                "web_name": p["web_name"],
                "team_short": team_short_by_id.get(p["team"], "UNK"),
                "position": POSITION_CODES.get(p["element_type"], "UNK"),
                "now_cost": round(p["now_cost"] / 10, 1),
            }
        )
    return players


def fetch_team_transfers(team_id: int) -> list[dict]:
    """Returns every transfer a manager has ever made.

    FPL returns the manager's FULL transfer history on every call (not
    just recent ones), so this naturally backfills the whole season on
    first run. Subsequent runs re-fetch the same list; the unique
    constraint on (team_id, transfer_time, element_in, element_out)
    means re-upserting is a safe no-op for anything already stored.
    """
    url = f"{FPL_BASE}/entry/{team_id}/transfers/"
    data = fetch_json(url)

    rows = []
    for t in data:
        rows.append(
            {
                "team_id": team_id,
                "event": t["event"],
                "transfer_time": t["time"],
                "element_in": t["element_in"],
                "element_in_cost": round(t["element_in_cost"] / 10, 1),
                "element_out": t["element_out"],
                "element_out_cost": round(t["element_out_cost"] / 10, 1),
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


def supabase_select(table: str, columns: str) -> list[dict]:
    """Simple paginated-free select — fine for tables in the hundreds/low
    thousands of rows like `players`. Would need range headers for anything
    bigger."""
    url = f"{SUPABASE_URL}/rest/v1/{table}?select={columns}"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def sync_players_and_detect_price_changes(current_players: list[dict]) -> None:
    """Diffs today's prices against what's stored, logs any changes, then
    overwrites the stored state with today's prices."""
    existing = supabase_select("players", "player_id,now_cost")
    old_price_by_id = {row["player_id"]: row["now_cost"] for row in existing}

    today = datetime.date.today().isoformat()
    changes = []
    for player in current_players:
        old_price = old_price_by_id.get(player["player_id"])
        new_price = player["now_cost"]
        if old_price is not None and old_price != new_price:
            changes.append(
                {
                    "player_id": player["player_id"],
                    "change_date": today,
                    "old_price": old_price,
                    "new_price": new_price,
                    "direction": "rise" if new_price > old_price else "fall",
                }
            )

    if changes:
        print(f"Detected {len(changes)} price change(s) today.")
        supabase_upsert("price_changes", changes, on_conflict="player_id,change_date")
    else:
        print("No price changes detected today.")

    supabase_upsert("players", current_players, on_conflict="player_id")


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
    all_transfers = []
    for team in teams:
        print(f"  fetching history for {team['team_name']} ({team['team_id']})...")
        all_snapshots.extend(fetch_team_history(team["team_id"]))
        all_transfers.extend(fetch_team_transfers(team["team_id"]))

    print(f"Upserting {len(all_snapshots)} gameweek snapshot rows...")
    supabase_upsert("gameweek_snapshots", all_snapshots, on_conflict="gw,team_id")

    print(f"Upserting {len(all_transfers)} transfer rows...")
    supabase_upsert(
        "transfers", all_transfers, on_conflict="team_id,transfer_time,element_in,element_out"
    )

    print("Fetching all player prices...")
    all_players = fetch_all_players()
    print(f"Found {len(all_players)} players. Checking for price changes...")
    sync_players_and_detect_price_changes(all_players)

    print("Done.")


if __name__ == "__main__":
    main()
