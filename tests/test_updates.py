import datetime as dt
import unittest
from unittest.mock import patch

from run_update import validate_target, main
from update_state import claim_email


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.bootstrap = {"events": [
            {"id": 1, "deadline_time": "2026-08-14T17:00:00Z"},
            {"id": 8, "finished": True, "data_checked": True},
        ]}
        self.fixtures = [{"event": 8, "finished": True, "kickoff_time": "2026-11-09T20:00:00Z"}]

    def validate(self, now):
        validate_target(self.bootstrap, self.fixtures, "2026", 8,
                        dt.datetime.fromisoformat(now))

    def test_not_before_nine(self):
        with self.assertRaises(ValueError):
            self.validate("2026-11-10T08:59:00+00:00")
        self.validate("2026-11-10T09:00:00+00:00")

    def test_summer_nine(self):
        self.fixtures[0]["kickoff_time"] = "2026-08-16T15:30:00Z"
        with self.assertRaises(ValueError):
            self.validate("2026-08-17T07:59:00+00:00")
        self.validate("2026-08-17T08:00:00+00:00")

    def test_unfinished_or_unchecked_rejected(self):
        self.fixtures[0]["finished"] = False
        with self.assertRaises(ValueError):
            self.validate("2026-11-10T09:00:00+00:00")
        self.fixtures[0]["finished"] = True
        self.bootstrap["events"][1]["data_checked"] = False
        with self.assertRaises(ValueError):
            self.validate("2026-11-10T09:00:00+00:00")

    def test_wrong_season_rejected(self):
        with self.assertRaises(ValueError):
            validate_target(self.bootstrap, self.fixtures, "2025", 8,
                            dt.datetime(2026, 11, 10, 9, tzinfo=dt.timezone.utc))

    @patch("update_state.state_request")
    def test_already_sent_email_skipped(self, request):
        request.return_value = [{"status": "sent"}]
        self.assertFalse(claim_email("2026", 8, 42))
        request.assert_called_once()

    @patch("update_state.state_request")
    def test_uncertain_email_never_resent_automatically(self, request):
        request.return_value = [{"status": "sending"}]
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            claim_email("2026", 8, 42)
        request.assert_called_once()

    @patch("update_state.state_request")
    def test_new_email_claimed_before_delivery(self, request):
        request.side_effect = [[], [{"status": "sending"}]]
        self.assertTrue(claim_email("2026", 8, 42))
        self.assertEqual(request.call_args.args, ("POST", "gw_email_deliveries"))

    @patch.dict("os.environ", {"TARGET_GAMEWEEK": "8", "TARGET_SEASON": "2026"})
    @patch("run_update.validate_target")
    @patch("run_update.collector.fetch_json", return_value={})
    @patch("run_update.completed", return_value=True)
    @patch("run_update.subprocess.run")
    def test_completed_update_does_not_run_scripts(self, run, completed, fetch, validate):
        main()
        run.assert_not_called()

    @patch.dict("os.environ", {"TARGET_GAMEWEEK": "8", "TARGET_SEASON": "2026"})
    @patch("run_update.validate_target")
    @patch("run_update.collector.fetch_json", return_value={})
    @patch("run_update.completed", return_value=False)
    @patch("run_update.state_request")
    @patch("run_update.subprocess.run")
    def test_missing_snapshots_block_cups_and_emails(self, run, request, completed, fetch, validate):
        request.side_effect = [[{"team_id": 42}], []]
        with self.assertRaisesRegex(RuntimeError, "Incomplete snapshots"):
            main()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][-1], "collector.py")

    @patch.dict("os.environ", {"TARGET_GAMEWEEK": "8", "TARGET_SEASON": "2026"})
    @patch("run_update.validate_target")
    @patch("run_update.collector.fetch_json", return_value={})
    @patch("run_update.completed", return_value=False)
    @patch("run_update.state_request")
    @patch("run_update.subprocess.run")
    def test_completion_written_only_after_all_steps(self, run, request, completed, fetch, validate):
        request.side_effect = [[{"team_id": 42}], [{"team_id": 42}], []]
        main()
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args.args[0][-4:], ["--gameweek", "8", "--season", "2026"])
        self.assertEqual(request.call_args.args, ("POST", "gw_update_runs"))


if __name__ == "__main__":
    unittest.main()
