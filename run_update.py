"""Run one finalized FPL gameweek, safely repeatable through GitHub concurrency.

Two ways to run:
  * Manual: TARGET_GAMEWEEK (and optionally TARGET_SEASON) set -> process exactly
    that gameweek, failing loudly if FPL hasn't finalized it or it isn't due yet.
  * Auto (scheduled, or manual with the inputs left blank): work out the NEXT
    gameweek to process (the one after the last completed gameweek recorded in
    gw_update_runs). If it isn't finalized/due yet, exit cleanly with nothing to
    do, so hourly scheduled checks don't show up as failures.
"""
import datetime as dt
import os
import subprocess
import sys
from zoneinfo import ZoneInfo

import collector
from update_state import completed, state_request


def current_season(bootstrap):
    """Season start year, e.g. '2026' for 2026/27 (year of the GW1 deadline)."""
    return str(dt.datetime.fromisoformat(
        bootstrap["events"][0]["deadline_time"].replace("Z", "+00:00")
    ).year)


def validate_target(bootstrap, fixtures, season, gw, now):
    actual_season = current_season(bootstrap)
    if season != actual_season:
        raise ValueError("Requested season does not match the current FPL season")
    event = next((e for e in bootstrap["events"] if e["id"] == gw), None)
    matches = [f for f in fixtures if f["event"] == gw]
    if not event or not event["finished"] or not event["data_checked"]:
        raise ValueError("Gameweek is not finalized by FPL yet")
    if not matches or any(not f["finished"] or not f["kickoff_time"] for f in matches):
        raise ValueError("Gameweek has unfinished or undated fixtures")
    london = ZoneInfo("Europe/London")
    last = max(dt.datetime.fromisoformat(f["kickoff_time"].replace("Z", "+00:00")) for f in matches)
    next_day = last.astimezone(london).date() + dt.timedelta(days=1)
    due = dt.datetime.combine(next_day, dt.time(9), tzinfo=london)
    if now < due:
        raise ValueError("Update is not due until 09:00 UK time after the last fixture")


def last_completed_gw(season):
    """Highest gameweek recorded as completed for this season, or None."""
    rows = state_request("GET", "gw_update_runs", params={
        "season": f"eq.{season}", "select": "gw", "order": "gw.desc", "limit": "1",
    })
    return rows[0]["gw"] if rows else None


def choose_target(bootstrap, season, last_done):
    """Which gameweek should an automatic run process? Returns an int, or None
    if there is nothing to do yet. Raises RuntimeError in states where guessing
    could email managers about old gameweeks."""
    finalized = [e["id"] for e in bootstrap["events"] if e["finished"] and e["data_checked"]]
    latest = max(finalized) if finalized else 0

    if last_done is None:
        if latest == 0:
            return None  # season hasn't produced a finalized gameweek yet
        if latest == 1:
            candidate = 1  # first gameweek of a fresh season: safe to start
        else:
            raise RuntimeError(
                f"No completed gameweeks recorded for season {season}, but FPL has "
                f"finalized up to GW{latest}. Refusing to guess (it could email "
                f"managers about old gameweeks). Run once manually with the "
                f"gameweek you want to start from, then automatic runs take over."
            )
    else:
        candidate = last_done + 1

    if candidate > 38:
        return None
    event = next((e for e in bootstrap["events"] if e["id"] == candidate), None)
    if not event or not event["finished"] or not event["data_checked"]:
        return None
    return candidate


def main():
    gw_env = os.environ.get("TARGET_GAMEWEEK", "").strip()
    season_env = os.environ.get("TARGET_SEASON", "").strip()
    auto = not gw_env

    bootstrap = collector.fetch_json(f"{collector.FPL_BASE}/bootstrap-static/")
    season = season_env or current_season(bootstrap)

    if auto:
        gw = choose_target(bootstrap, season, last_completed_gw(season))
        if gw is None:
            print(f"{season}: no newly finalized gameweek to process; nothing to do")
            return
        print(f"{season}: next gameweek to process is GW{gw}")
    else:
        gw = int(gw_env)
        if not 1 <= gw <= 38:
            raise ValueError("Gameweek must be between 1 and 38")

    fixtures = collector.fetch_json(f"{collector.FPL_BASE}/fixtures/")
    try:
        validate_target(bootstrap, fixtures, season, gw, dt.datetime.now(dt.timezone.utc))
    except ValueError as e:
        if auto:
            print(f"{season} GW{gw} not ready yet: {e}; nothing to do")
            return
        raise

    if completed(season, gw):
        print(f"{season} GW{gw} already completed; skipping")
        return
    subprocess.run([sys.executable, "collector.py"], check=True)
    teams = state_request("GET", "teams", params={"select": "team_id"})
    snapshots = state_request("GET", "gameweek_snapshots", params={
        "select": "team_id", "gw": f"eq.{gw}",
    })
    if not teams or not {t["team_id"] for t in teams}.issubset({s["team_id"] for s in snapshots}):
        raise RuntimeError(f"Incomplete snapshots for GW{gw}; refusing to process cups or send emails")
    for args in (["cups.py"],
                 ["email_notifier.py", "--gameweek", str(gw), "--season", season]):
        subprocess.run([sys.executable, *args], check=True)
    state_request("POST", "gw_update_runs", row={"season": season, "gw": gw})
    print(f"{season} GW{gw} completed")


if __name__ == "__main__":
    main()
