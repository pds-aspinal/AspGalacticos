"""Tests for run_update.py auto mode. No network: FPL + Supabase are mocked.
Run: python3 test_run_update.py
"""
import datetime as dt
import os
import sys
from unittest.mock import patch

sys.path.insert(0, ".")
import run_update as ru

passed = failed = 0


def check(label, cond):
    global passed, failed
    if cond:
        passed += 1
        print("  OK  ", label)
    else:
        failed += 1
        print("  FAIL", label)


def raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


def bootstrap(finalized_upto, season_year=2026):
    events = []
    for i in range(1, 39):
        done = i <= finalized_upto
        events.append({
            "id": i,
            "deadline_time": f"{season_year}-08-15T17:30:00Z" if i == 1 else f"{season_year}-09-01T10:00:00Z",
            "finished": done, "data_checked": done,
        })
    return {"events": events}


def fixtures_for(gw, last_kickoff="2026-11-09T20:00:00Z", finished=True):
    return [
        {"event": gw, "finished": finished, "kickoff_time": "2026-11-07T15:00:00Z"},
        {"event": gw, "finished": finished, "kickoff_time": last_kickoff},
    ]


print("=== current_season ===")
check("season from GW1 deadline year", ru.current_season(bootstrap(5)) == "2026")
check("works for a different year", ru.current_season(bootstrap(5, 2027)) == "2027")

print("\n=== choose_target ===")
check("picks the gameweek after the last completed one",
      ru.choose_target(bootstrap(12), "2026", last_done=10) == 11)
check("None when the next gameweek isn't finalized yet",
      ru.choose_target(bootstrap(10), "2026", last_done=10) is None)
check("None when the season is complete (last_done=38)",
      ru.choose_target(bootstrap(38), "2026", last_done=38) is None)
check("fresh season: GW1 finalized and nothing recorded -> start at 1",
      ru.choose_target(bootstrap(1), "2026", last_done=None) == 1)
check("nothing finalized and nothing recorded -> None",
      ru.choose_target(bootstrap(0), "2026", last_done=None) is None)
check("mid-season with an empty state table refuses to guess (no old-GW emails)",
      raises(lambda: ru.choose_target(bootstrap(10), "2026", last_done=None), RuntimeError))
check("never goes backwards: ignores older finalized gameweeks",
      ru.choose_target(bootstrap(20), "2026", last_done=19) == 20)

print("\n=== validate_target (unchanged behaviour) ===")
now_due = dt.datetime(2026, 11, 10, 9, 5, tzinfo=dt.timezone.utc)   # 09:05 UTC Tue, after 09:00 UK
now_early = dt.datetime(2026, 11, 10, 7, 0, tzinfo=dt.timezone.utc)  # before 09:00 UK
check("passes once due", not raises(lambda: ru.validate_target(bootstrap(11), fixtures_for(11), "2026", 11, now_due), ValueError)
      and ru.validate_target(bootstrap(11), fixtures_for(11), "2026", 11, now_due) is None)
check("rejects before 09:00 UK next day",
      raises(lambda: ru.validate_target(bootstrap(11), fixtures_for(11), "2026", 11, now_early), ValueError))
check("rejects unfinished fixtures",
      raises(lambda: ru.validate_target(bootstrap(11), fixtures_for(11, finished=False), "2026", 11, now_due), ValueError))
check("rejects wrong season",
      raises(lambda: ru.validate_target(bootstrap(11), fixtures_for(11), "2025", 11, now_due), ValueError))


class Recorder:
    """Stands in for subprocess.run + state_request + completed + fetch_json."""
    def __init__(self, boot, fixtures, last_done, already_completed=False):
        self.boot, self.fixtures, self.last_done = boot, fixtures, last_done
        self.already_completed = already_completed
        self.ran = []
        self.posted = []

    def fetch_json(self, url):
        return self.boot if "bootstrap-static" in url else self.fixtures

    def subprocess_run(self, args, check=False):
        self.ran.append(args[1:])

    def state_request(self, method, table, params=None, row=None):
        if table == "gw_update_runs" and method == "GET":
            return [{"gw": self.last_done}] if self.last_done is not None else []
        if table == "teams":
            return [{"team_id": 1}, {"team_id": 2}]
        if table == "gameweek_snapshots":
            return [{"team_id": 1}, {"team_id": 2}]
        if table == "gw_update_runs" and method == "POST":
            self.posted.append(row)
            return []
        return []

    def completed(self, season, gw):
        return self.already_completed


def run_main(rec, env, now):
    class FakeDT(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    with patch.dict(os.environ, env, clear=False), \
         patch.object(ru.collector, "fetch_json", rec.fetch_json), \
         patch.object(ru.subprocess, "run", rec.subprocess_run), \
         patch.object(ru, "state_request", rec.state_request), \
         patch.object(ru, "completed", rec.completed), \
         patch.object(ru.dt, "datetime", FakeDT):
        ru.main()


AUTO = {"TARGET_GAMEWEEK": "", "TARGET_SEASON": ""}

print("\n=== main(): auto mode ===")
rec = Recorder(bootstrap(11), fixtures_for(11), last_done=10)
run_main(rec, AUTO, now_due)
check("processes GW11: collector, then cups, then emails (in order)",
      rec.ran == [["collector.py"], ["cups.py"], ["email_notifier.py", "--gameweek", "11", "--season", "2026"]])
check("records GW11 as completed", rec.posted == [{"season": "2026", "gw": 11}])

rec = Recorder(bootstrap(11), fixtures_for(11), last_done=10)
run_main(rec, AUTO, now_early)
check("not due yet -> exits cleanly, runs nothing", rec.ran == [] and rec.posted == [])

rec = Recorder(bootstrap(10), fixtures_for(10), last_done=10)
run_main(rec, AUTO, now_due)
check("nothing new finalized -> exits cleanly, runs nothing", rec.ran == [] and rec.posted == [])

rec = Recorder(bootstrap(11), fixtures_for(11), last_done=10, already_completed=True)
run_main(rec, AUTO, now_due)
check("already completed -> skips (idempotent)", rec.ran == [] and rec.posted == [])

rec = Recorder(bootstrap(14), fixtures_for(11), last_done=10)
run_main(rec, AUTO, now_due)
check("backlog: handles one gameweek per run (GW11 only, not 12-14)",
      ["email_notifier.py", "--gameweek", "11", "--season", "2026"] in rec.ran
      and not any("12" in a for a in rec.ran) and rec.posted == [{"season": "2026", "gw": 11}])

rec = Recorder(bootstrap(14), fixtures_for(11), last_done=None)
check("empty state mid-season fails loudly, nothing sent",
      raises(lambda: run_main(rec, AUTO, now_due), RuntimeError) and rec.ran == [])

print("\n=== main(): manual mode (must behave as before) ===")
rec = Recorder(bootstrap(11), fixtures_for(11), last_done=10)
run_main(rec, {"TARGET_GAMEWEEK": "11", "TARGET_SEASON": "2026"}, now_due)
check("manual GW11 processes it", rec.posted == [{"season": "2026", "gw": 11}])

rec = Recorder(bootstrap(10), fixtures_for(11), last_done=10)
check("manual GW11 not finalized -> still raises (loud)",
      raises(lambda: run_main(rec, {"TARGET_GAMEWEEK": "11", "TARGET_SEASON": "2026"}, now_due), ValueError))

rec = Recorder(bootstrap(11), fixtures_for(11), last_done=10)
run_main(rec, {"TARGET_GAMEWEEK": "11", "TARGET_SEASON": ""}, now_due)
check("manual with season left blank uses the current season", rec.posted == [{"season": "2026", "gw": 11}])

rec = Recorder(bootstrap(11), fixtures_for(11), last_done=10)
check("manual out-of-range gameweek still rejected",
      raises(lambda: run_main(rec, {"TARGET_GAMEWEEK": "39", "TARGET_SEASON": "2026"}, now_due), ValueError))

print(f"\n{'=' * 40}\n{passed} passed, {failed} failed\n{'=' * 40}")
sys.exit(1 if failed else 0)
