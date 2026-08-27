"""
Cup automation driver — runs after collector.py each week.

Simulates Cup 1 (Last Man Standing) and Cup 2 (elimination -> random-drawn
semi-final -> final) from the scores collector.py has already gathered.
Fetches Cup 3 (FPL's own automated H2H cup) directly from FPL, since that
tournament is run by FPL itself, not something we compute.

Requires the same three environment variables as collector.py:
  SUPABASE_URL, SUPABASE_SERVICE_KEY, LEAGUE_ID

Run: python cups.py
"""

import os
import sys
import requests

from cup_logic import (
    simulate_elimination_cup, draw_random_pairs, resolve_head_to_head_match,
)

FPL_BASE = "https://fantasy.premierleague.com/api"
HEADERS = {"User-Agent": "Mozilla/5.0 (fpl-league-tracker)"}

CUP1_NAME = "Cup 1 — LMS Cup"
CUP2_NAME = "Cup 2 — Champion of Champions"
CUP3_NAME = "Cup 3 — FPL League Cup"

CUP1_START_GW = 8
CUP2_START_GW = 23
CUP3_START_GW = 35


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


# ── Supabase helpers ──────────────────────────────────────────────

def sb_get(table: str, query: str = "") -> list:
    url = f"{SUPABASE_URL}/rest/v1/{table}?{query}"
    headers = {"apikey": SUPABASE_SERVICE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}"}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def sb_upsert(table: str, rows: list, on_conflict: str) -> None:
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


# ── Goals-based tiebreak data (fetched lazily, only when actually needed) ──

def fetch_goals_for_teams(team_ids: list, gw: int) -> tuple[dict, dict]:
    """
    Returns ({team_id: goals_scored}, {team_id: goals_conceded}) for a
    specific gameweek, using each player's RAW goals (not captain-doubled —
    "goals scored" as a tiebreak is treated as a literal goal count, distinct
    from FPL points). Caches to gw_goals_stats so a repeat tiebreak lookup
    (or a future tie in the same gameweek) doesn't re-fetch.
    """
    print(f"  Resolving tiebreak: fetching goals data for {len(team_ids)} teams, GW{gw}...")

    # Check cache first
    cached = sb_get("gw_goals_stats", f"select=*&gw=eq.{gw}&team_id=in.({','.join(str(t) for t in team_ids)})")
    cached_ids = {row["team_id"] for row in cached}
    goals_scored = {row["team_id"]: row["goals_scored"] for row in cached}
    goals_conceded = {row["team_id"]: row["goals_conceded"] for row in cached}

    missing = [t for t in team_ids if t not in cached_ids]
    if not missing:
        return goals_scored, goals_conceded

    live = fetch_json(f"{FPL_BASE}/event/{gw}/live/")
    player_stats = {el["id"]: el["stats"] for el in live["elements"]}

    new_rows = []
    for team_id in missing:
        picks_data = fetch_json(f"{FPL_BASE}/entry/{team_id}/event/{gw}/picks/")
        counted_player_ids = [p["element"] for p in picks_data["picks"] if p["multiplier"] > 0]
        scored = sum(player_stats.get(pid, {}).get("goals_scored", 0) for pid in counted_player_ids)
        conceded = sum(player_stats.get(pid, {}).get("goals_conceded", 0) for pid in counted_player_ids)
        goals_scored[team_id] = scored
        goals_conceded[team_id] = conceded
        new_rows.append({"team_id": team_id, "gw": gw, "goals_scored": scored, "goals_conceded": conceded})

    sb_upsert("gw_goals_stats", new_rows, on_conflict="team_id,gw")
    return goals_scored, goals_conceded


# ── Cup 1 & 2: elimination-style rounds ──────────────────────────

def load_scores(team_ids: list) -> dict:
    """{(team_id, gw): points} for every team/gw we've collected so far."""
    snaps = sb_get("gameweek_snapshots", f"select=team_id,gw,gw_points&team_id=in.({','.join(str(t) for t in team_ids)})")
    return {(s["team_id"], s["gw"]): s["gw_points"] for s in snaps if s["gw_points"] is not None}


def latest_collected_gw(scores: dict) -> int:
    return max((gw for (_, gw) in scores.keys()), default=0)


def run_cup1(all_team_ids: list, scores: dict, latest_gw: int) -> None:
    print(f"\n=== {CUP1_NAME} ===")
    existing = sb_get("cup_eliminations", f"select=*&cup_name=eq.{CUP1_NAME.replace(' ', '%20')}")
    already = {row["team_id"]: (row["gw"], row["score"], row["tiebreak_used"]) for row in existing}
    existing_winner = sb_get("cup_winners", f"select=*&cup_name=eq.{CUP1_NAME.replace(' ', '%20')}")
    if existing_winner:
        print(f"  Already has a winner (team {existing_winner[0]['team_id']}) — nothing more to do.")
        return

    new_elims, active, winner = simulate_elimination_cup(
        CUP1_NAME, all_team_ids, CUP1_START_GW, latest_gw,
        scores, already, fetch_goals_for_teams, stop_at_n_remaining=1,
    )
    if new_elims:
        print(f"  {len(new_elims)} new elimination(s): " + ", ".join(f"team {e['team_id']} (GW{e['gw']}, {e['score']}pts)" for e in new_elims))
        sb_upsert("cup_eliminations", new_elims, on_conflict="cup_name,team_id")
    else:
        print("  No new rounds to process yet.")

    if winner:
        print(f"  🏆 Winner: team {winner}")
        sb_upsert("cup_winners", [{"cup_name": CUP1_NAME, "team_id": winner, "won_gw": latest_gw}], on_conflict="cup_name")


def run_cup2(all_team_ids: list, scores: dict, latest_gw: int) -> None:
    print(f"\n=== {CUP2_NAME} ===")
    existing_winner = sb_get("cup_winners", f"select=*&cup_name=eq.{CUP2_NAME.replace(' ', '%20')}")
    if existing_winner:
        print(f"  Already has a winner (team {existing_winner[0]['team_id']}) — nothing more to do.")
        return

    existing = sb_get("cup_eliminations", f"select=*&cup_name=eq.{CUP2_NAME.replace(' ', '%20')}")
    already = {row["team_id"]: (row["gw"], row["score"], row["tiebreak_used"]) for row in existing}

    # Phase 1: elimination down to 4
    new_elims, active, _ = simulate_elimination_cup(
        CUP2_NAME, all_team_ids, CUP2_START_GW, latest_gw,
        scores, already, fetch_goals_for_teams, stop_at_n_remaining=4,
    )
    if new_elims:
        print(f"  {len(new_elims)} new elimination(s): " + ", ".join(f"team {e['team_id']} (GW{e['gw']}, {e['score']}pts)" for e in new_elims))
        sb_upsert("cup_eliminations", new_elims, on_conflict="cup_name,team_id")

    if len(active) > 4:
        print(f"  Still {len(active)} teams active — not yet down to the final 4.")
        return
    if len(active) < 4:
        print(f"  WARNING: {len(active)} teams active, expected exactly 4 — check for a data issue.", file=sys.stderr)
        return

    # Phase 2: semi-final (random draw, done ONCE and then locked in)
    existing_matches = sb_get("cup_matches", f"select=*&cup_name=eq.{CUP2_NAME.replace(' ', '%20')}")
    semis = [m for m in existing_matches if m["round_label"] == "Semi-Final"]

    if not semis:
        pairs = draw_random_pairs(active, CUP2_NAME)
        print(f"  Final 4 reached — drawing semi-finals: {pairs[0]} vs {pairs[1]}")
        semi_gw = latest_gw + 1  # semis play out the gameweek after the draw
        placeholder_rows = [
            {"cup_name": CUP2_NAME, "round_label": "Semi-Final", "gw": semi_gw,
             "team1_id": p[0], "team2_id": p[1]}
            for p in pairs
        ]
        sb_upsert("cup_matches", placeholder_rows, on_conflict="cup_name,round_label,team1_id,team2_id")
        return  # wait for that gameweek's scores before resolving the result

    # Resolve any semis whose gameweek has now happened but result isn't recorded yet
    unresolved_semis = [m for m in semis if m.get("winner_team_id") is None]
    for m in unresolved_semis:
        result = resolve_head_to_head_match(m["team1_id"], m["team2_id"], m["gw"], scores, fetch_goals_for_teams, CUP2_NAME, "Semi-Final")
        if result:
            print(f"  Semi-final result: team {result['team1_id']} {result['team1_score']} - {result['team2_score']} team {result['team2_id']} -> winner: team {result['winner_team_id']}")
            sb_upsert("cup_matches", [result], on_conflict="cup_name,round_label,team1_id,team2_id")

    semis = sb_get("cup_matches", f"select=*&cup_name=eq.{CUP2_NAME.replace(' ', '%20')}&round_label=eq.Semi-Final")
    if any(m.get("winner_team_id") is None for m in semis):
        print("  Semi-finals drawn but not all results are in yet.")
        return

    # Phase 3: final
    finals = [m for m in existing_matches if m["round_label"] == "Final"]
    if not finals:
        finalists = [m.get("winner_team_id") for m in semis]
        final_gw = max(m["gw"] for m in semis) + 1
        print(f"  Semi-finals complete — final: team {finalists[0]} vs team {finalists[1]} (GW{final_gw})")
        sb_upsert("cup_matches", [{
            "cup_name": CUP2_NAME, "round_label": "Final", "gw": final_gw,
            "team1_id": finalists[0], "team2_id": finalists[1],
        }], on_conflict="cup_name,round_label,team1_id,team2_id")
        return

    final = finals[0]
    if final.get("winner_team_id") is None:
        result = resolve_head_to_head_match(final["team1_id"], final["team2_id"], final["gw"], scores, fetch_goals_for_teams, CUP2_NAME, "Final")
        if result:
            print(f"  FINAL result: team {result['team1_id']} {result['team1_score']} - {result['team2_score']} team {result['team2_id']}")
            sb_upsert("cup_matches", [result], on_conflict="cup_name,round_label,team1_id,team2_id")
            print(f"  🏆 Winner: team {result['winner_team_id']}")
            sb_upsert("cup_winners", [{"cup_name": CUP2_NAME, "team_id": result["winner_team_id"], "won_gw": final["gw"]}], on_conflict="cup_name")


# ── Cup 3: FPL's own automated H2H cup ───────────────────────────

def run_cup3(all_team_ids: list) -> None:
    """
    FPL generates this cup's fixtures/results itself — we just pull and
    store them, no simulation.

    NOTE: this endpoint (/entry/{id}/cup/) has NOT been verified live from
    this environment (no network access to fantasy.premierleague.com here).
    If this fails or the field names don't match what's below, that's
    expected on the first real run — check the printed error, and the
    field names likely just need adjusting to match what FPL actually
    returns for your league.
    """
    print(f"\n=== {CUP3_NAME} ===")
    existing_matches = sb_get("cup_matches", f"select=*&cup_name=eq.{CUP3_NAME.replace(' ', '%20')}")
    existing_keys = {(m["round_label"], m["team1_id"], m["team2_id"]) for m in existing_matches}

    new_rows = []
    for team_id in all_team_ids:
        try:
            data = fetch_json(f"{FPL_BASE}/entry/{team_id}/cup/")
        except requests.exceptions.RequestException as e:
            print(f"  Could not fetch cup data for team {team_id}: {e}", file=sys.stderr)
            continue

        matches = data.get("matches", data.get("cup_matches", []))
        if not matches:
            continue

        for m in matches:
            entry1 = m.get("entry_1_entry")
            entry2 = m.get("entry_2_entry")
            if entry1 is None or entry2 is None:
                continue
            round_label = f"Round {m.get('event', '?')}"
            key = (round_label, entry1, entry2)
            key_rev = (round_label, entry2, entry1)
            if key in existing_keys or key_rev in existing_keys:
                continue

            winner = None
            if m.get("winner"):
                winner = m["winner"]
            elif m.get("entry_1_points") is not None and m.get("entry_2_points") is not None:
                if m["entry_1_points"] > m["entry_2_points"]:
                    winner = entry1
                elif m["entry_2_points"] > m["entry_1_points"]:
                    winner = entry2

            new_rows.append({
                "cup_name": CUP3_NAME, "round_label": round_label, "gw": m.get("event"),
                "team1_id": entry1, "team2_id": entry2,
                "team1_score": m.get("entry_1_points"), "team2_score": m.get("entry_2_points"),
                "winner_team_id": winner,
            })
            existing_keys.add(key)

    if new_rows:
        print(f"  {len(new_rows)} new/updated match(es) found.")
        sb_upsert("cup_matches", new_rows, on_conflict="cup_name,round_label,team1_id,team2_id")
    else:
        print("  No new Cup 3 results found (either nothing new, or the endpoint didn't return what was expected — see above).")


def main():
    global SUPABASE_URL, SUPABASE_SERVICE_KEY
    SUPABASE_URL = env("SUPABASE_URL").rstrip("/")
    SUPABASE_SERVICE_KEY = env("SUPABASE_SERVICE_KEY")
    league_id = env("LEAGUE_ID")

    teams = sb_get("teams", "select=team_id")
    all_team_ids = [t["team_id"] for t in teams]
    print(f"Loaded {len(all_team_ids)} teams.")

    scores = load_scores(all_team_ids)
    latest_gw = latest_collected_gw(scores)
    print(f"Latest gameweek with collected scores: GW{latest_gw}")

    if latest_gw >= CUP1_START_GW:
        run_cup1(all_team_ids, scores, latest_gw)
    else:
        print(f"\n{CUP1_NAME} hasn't started yet (starts GW{CUP1_START_GW}).")

    if latest_gw >= CUP2_START_GW:
        run_cup2(all_team_ids, scores, latest_gw)
    else:
        print(f"\n{CUP2_NAME} hasn't started yet (starts GW{CUP2_START_GW}).")

    if latest_gw >= CUP3_START_GW:
        try:
            run_cup3(all_team_ids)
        except Exception as e:
            # Cup 3's endpoint hasn't been verified live — don't let a surprise
            # here fail the whole script and mask real Cup 1/2 results.
            print(f"\n{CUP3_NAME}: FAILED — {e}", file=sys.stderr)
            print("This endpoint hasn't been confirmed working live yet — see the note in run_cup3().", file=sys.stderr)
    else:
        print(f"\n{CUP3_NAME} hasn't started yet (starts GW{CUP3_START_GW}).")

    print("\nDone.")


if __name__ == "__main__":
    main()
