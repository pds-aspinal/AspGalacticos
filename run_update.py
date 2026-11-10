"""Run one finalized FPL gameweek, safely repeatable through GitHub concurrency."""
import datetime as dt
import os
import subprocess
import sys
from zoneinfo import ZoneInfo

import collector
from update_state import completed, state_request


def validate_target(bootstrap, fixtures, season, gw, now):
    actual_season = str(dt.datetime.fromisoformat(
        bootstrap["events"][0]["deadline_time"].replace("Z", "+00:00")
    ).year)
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


def main():
    gw = int(os.environ["TARGET_GAMEWEEK"])
    season = os.environ["TARGET_SEASON"]
    if not 1 <= gw <= 38:
        raise ValueError("Gameweek must be between 1 and 38")
    bootstrap = collector.fetch_json(f"{collector.FPL_BASE}/bootstrap-static/")
    fixtures = collector.fetch_json(f"{collector.FPL_BASE}/fixtures/")
    validate_target(bootstrap, fixtures, season, gw, dt.datetime.now(dt.timezone.utc))
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
