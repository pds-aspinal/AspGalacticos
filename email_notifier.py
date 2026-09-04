"""
email_notifier.py

Sends a personalised "GW recap + next GW preview" email to every active
manager in the Aspinal Galacticos League, after collector.py has run.

Data sources:
  - Supabase (teams, gameweek_snapshots, manager_emails) -> this GW's recap
  - FPL public API (bootstrap-static, fixtures, entry picks) -> next GW preview

Sends via Gmail SMTP using an App Password (works fine from GitHub Actions,
no OAuth flow needed). Requires 2-Step Verification to be on for the Gmail
account, with an App Password generated for "Mail".

Environment variables required:
  SUPABASE_URL                 e.g. https://lsasffymfvshobtbihly.supabase.co
  SUPABASE_SERVICE_ROLE_KEY    service role key (NOT anon key - needs write-level read access)
  GMAIL_ADDRESS                the sending Gmail address
  GMAIL_APP_PASSWORD           16-character app password (no spaces)

Usage:
  python email_notifier.py                 # normal run, sends to all active managers
  python email_notifier.py --dry-run       # builds emails, prints them, sends nothing
  python email_notifier.py --test-to me@x.com   # sends ALL emails to one address instead
                                                  (subject line tags which manager it was for)
"""

import os
import sys
import time
import argparse
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

FPL_BASE = "https://fantasy.premierleague.com/api"
DASHBOARD_URL = "https://aspgalacticos.pages.dev"

CHIP_NAMES = {
    "wildcard": "Wildcard",
    "freehit": "Free Hit",
    "bboost": "Bench Boost",
    "3xc": "Triple Captain",
}


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def sb_headers(service_key):
    return {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
    }


def sb_get(supabase_url, service_key, table, params=None):
    resp = requests.get(
        f"{supabase_url}/rest/v1/{table}",
        headers=sb_headers(service_key),
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def get_current_gw(supabase_url, service_key):
    rows = sb_get(
        supabase_url, service_key, "gameweek_snapshots",
        params={"select": "gw", "order": "gw.desc", "limit": "1"},
    )
    if not rows:
        raise RuntimeError("No rows in gameweek_snapshots yet - has collector.py run at least once?")
    return rows[0]["gw"]


def get_teams(supabase_url, service_key):
    rows = sb_get(supabase_url, service_key, "teams", params={"select": "team_id,manager_name,team_name"})
    return {r["team_id"]: r for r in rows}


def get_manager_emails(supabase_url, service_key):
    rows = sb_get(
        supabase_url, service_key, "manager_emails",
        params={"select": "team_id,email", "active": "eq.true"},
    )
    return {r["team_id"]: r["email"] for r in rows}


def get_snapshots_for_gw(supabase_url, service_key, gw):
    rows = sb_get(
        supabase_url, service_key, "gameweek_snapshots",
        params={"select": "*", "gw": f"eq.{gw}"},
    )
    return {r["team_id"]: r for r in rows}


def rank_snapshots(snapshots_by_team):
    """Adds a computed 'rank' field based on total_points, matching how the
    dashboard itself must derive rank (there's no stored rank column)."""
    ranked = sorted(snapshots_by_team.values(), key=lambda r: r["total_points"], reverse=True)
    for i, row in enumerate(ranked, start=1):
        row["rank"] = i
    return {row["team_id"]: row for row in ranked}


# ---------------------------------------------------------------------------
# FPL API helpers
# ---------------------------------------------------------------------------

def fpl_get(path, params=None):
    resp = requests.get(f"{FPL_BASE}/{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_bootstrap():
    return fpl_get("bootstrap-static/")


def get_fixtures_for_event(event):
    return fpl_get("fixtures/", params={"event": event})


def get_entry_picks(team_id, gw):
    """Returns None if picks aren't available (e.g. invalid id, GW not started
    for that entry, or FPL API hiccup) rather than raising - one manager's
    missing data shouldn't kill the whole run."""
    try:
        return fpl_get(f"entry/{team_id}/event/{gw}/picks/")
    except requests.HTTPError:
        return None
    except requests.RequestException:
        return None


def group_fixtures_by_team(fixtures):
    by_team = {}
    for f in fixtures:
        by_team.setdefault(f["team_h"], []).append(f)
        by_team.setdefault(f["team_a"], []).append(f)
    return by_team


# ---------------------------------------------------------------------------
# Content builders
# ---------------------------------------------------------------------------

def format_delta(rank_now, rank_before):
    if rank_before is None:
        return ""
    diff = rank_before - rank_now
    if diff > 0:
        return f" (up {diff} \u25b2)"
    if diff < 0:
        return f" (down {abs(diff)} \u25bc)"
    return " (no change)"


def build_recap_section(team_id, teams, snap_now, snap_prev, all_snaps_now):
    team = teams[team_id]
    row = snap_now.get(team_id)
    if row is None:
        return f"<p>No snapshot found for {team['team_name']} this GW - skipping recap.</p>"

    prev_row = snap_prev.get(team_id) if snap_prev else None
    rank_delta_str = format_delta(row["rank"], prev_row["rank"] if prev_row else None)

    gw_points_all = [r["gw_points"] for r in all_snaps_now.values()]
    weekly_best = max(gw_points_all)
    weekly_worst = min(gw_points_all)

    lines = [
        f"<p><strong>GW points:</strong> {row['gw_points']}</p>",
        f"<p><strong>League rank:</strong> #{row['rank']}{rank_delta_str}</p>",
        f"<p><strong>Total points:</strong> {row['total_points']}</p>",
    ]

    if row.get("chip"):
        lines.append(f"<p><strong>Chip played:</strong> {CHIP_NAMES.get(row['chip'], row['chip'])}</p>")
    if row.get("transfer_cost"):
        lines.append(f"<p><strong>Transfer hit:</strong> -{row['transfer_cost']} pts</p>")

    if row["gw_points"] == weekly_best:
        lines.append("<p>\U0001F3C6 You had the <strong>highest score</strong> in the league this GW!</p>")
    elif row["gw_points"] == weekly_worst:
        lines.append(f"<p>Rough week - the league's high score was {weekly_best}.</p>")
    else:
        lines.append(f"<p>League this GW ranged from {weekly_worst} to {weekly_best} points.</p>")

    return "\n".join(lines)


def build_general_fdr_section(fixtures, teams_by_fpl_id, next_gw_number):
    if not fixtures:
        return f"<p>No fixtures found for GW{next_gw_number} yet (blank gameweek or fixtures not released).</p>"

    def fixture_label(f):
        h = teams_by_fpl_id[f["team_h"]]["short_name"]
        a = teams_by_fpl_id[f["team_a"]]["short_name"]
        return f"{h} vs {a} (FDR {f['team_h_difficulty']}/{f['team_a_difficulty']})"

    sorted_fixtures = sorted(fixtures, key=lambda f: f["team_h_difficulty"] + f["team_a_difficulty"])
    easiest = sorted_fixtures[:3]
    hardest = sorted_fixtures[-3:]

    lines = [f"<p><strong>GW{next_gw_number} at a glance:</strong></p>", "<ul>"]
    lines.append("<li>Easiest fixtures: " + ", ".join(fixture_label(f) for f in easiest) + "</li>")
    lines.append("<li>Toughest fixtures: " + ", ".join(fixture_label(f) for f in hardest) + "</li>")
    lines.append("</ul>")
    return "\n".join(lines)


def build_squad_fixture_section(team_id, next_gw_number, bootstrap, fixtures_by_team):
    picks_data = get_entry_picks(team_id, next_gw_number - 1)
    # NOTE: FPL's picks endpoint returns the squad picked FOR that gw; the most
    # recently completed gw's picks are the best available proxy for "your
    # current squad" until the next gw's picks are locked in.
    if not picks_data:
        return "<p>Couldn't retrieve your squad from the FPL API for a fixture breakdown this week.</p>"

    elements_by_id = {e["id"]: e for e in bootstrap["elements"]}
    teams_by_id = {t["id"]: t for t in bootstrap["teams"]}

    starters = [p for p in picks_data.get("picks", []) if p.get("multiplier", 0) > 0]
    if not starters:
        return "<p>No starting XI data available for a fixture breakdown this week.</p>"

    lines = [f"<p><strong>Your players' GW{next_gw_number} fixtures:</strong></p>", "<ul>"]
    for p in starters:
        el = elements_by_id.get(p["element"])
        if not el:
            continue
        team_short = teams_by_id[el["team"]]["short_name"]
        fixtures = fixtures_by_team.get(el["team"], [])
        if not fixtures:
            lines.append(f"<li>{el['web_name']} ({team_short}): no fixture (blank)</li>")
            continue
        parts = []
        for f in fixtures:
            is_home = f["team_h"] == el["team"]
            opp_id = f["team_a"] if is_home else f["team_h"]
            opp_short = teams_by_id[opp_id]["short_name"]
            difficulty = f["team_h_difficulty"] if is_home else f["team_a_difficulty"]
            venue = "H" if is_home else "A"
            parts.append(f"{opp_short} ({venue}, FDR {difficulty})")
        lines.append(f"<li>{el['web_name']} ({team_short}): {', '.join(parts)}</li>")
    lines.append("</ul>")
    return "\n".join(lines)


def build_email_html(manager_name, team_name, recap_html, fdr_html, squad_html, current_gw, next_gw):
    return f"""\
<html>
  <body style="font-family: Arial, sans-serif; color: #222; line-height: 1.5;">
    <h2>Aspinal Galacticos - GW{current_gw} Update</h2>
    <p>Hi {manager_name},</p>
    <p>Here's how <strong>{team_name}</strong> got on in GW{current_gw}, and what's coming up in GW{next_gw}.</p>

    <h3>GW{current_gw} Recap</h3>
    {recap_html}

    <h3>GW{next_gw} Preview</h3>
    {fdr_html}
    {squad_html}

    <p style="margin-top: 24px;">
      <a href="{DASHBOARD_URL}">View the full league dashboard</a>
    </p>
    <p style="color: #888; font-size: 12px;">
      Automated update from the Aspinal Galacticos League tracker.
    </p>
  </body>
</html>
"""


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def send_email(gmail_address, app_password, to_address, subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = to_address
    msg.attach(MIMEText(html_body, "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls(context=context)
        server.login(gmail_address, app_password)
        server.sendmail(gmail_address, to_address, msg.as_string())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Build emails but don't send anything")
    parser.add_argument("--test-to", default=None, help="Send every email to this address instead of real recipients")
    args = parser.parse_args()

    supabase_url = os.environ["SUPABASE_URL"]
    service_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    gmail_address = os.environ.get("GMAIL_ADDRESS")
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD")

    if not args.dry_run and not (gmail_address and gmail_app_password):
        sys.exit("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set unless using --dry-run")

    print("Fetching league data from Supabase...")
    current_gw = get_current_gw(supabase_url, service_key)
    next_gw = current_gw + 1
    teams = get_teams(supabase_url, service_key)
    emails = get_manager_emails(supabase_url, service_key)
    snap_now = rank_snapshots(get_snapshots_for_gw(supabase_url, service_key, current_gw))
    snap_prev_raw = get_snapshots_for_gw(supabase_url, service_key, current_gw - 1) if current_gw > 1 else {}
    snap_prev = rank_snapshots(snap_prev_raw) if snap_prev_raw else {}

    print(f"Current GW: {current_gw}. Fetching FPL fixture data for GW{next_gw}...")
    bootstrap = get_bootstrap()
    teams_by_fpl_id = {t["id"]: t for t in bootstrap["teams"]}
    fixtures = get_fixtures_for_event(next_gw)
    fixtures_by_team = group_fixtures_by_team(fixtures)

    sent, skipped, failed = 0, 0, 0

    for team_id, team in teams.items():
        to_address = emails.get(team_id)
        if not to_address:
            print(f"Skipping {team['team_name']} - no email on file.")
            skipped += 1
            continue

        recap_html = build_recap_section(team_id, teams, snap_now, snap_prev, snap_now)
        fdr_html = build_general_fdr_section(fixtures, teams_by_fpl_id, next_gw)
        squad_html = build_squad_fixture_section(team_id, next_gw, bootstrap, fixtures_by_team)

        subject = f"Aspinal Galacticos - GW{current_gw} Update: {team['team_name']}"
        body = build_email_html(
            team["manager_name"], team["team_name"], recap_html, fdr_html, squad_html, current_gw, next_gw
        )

        destination = args.test_to or to_address
        if args.test_to:
            subject = f"[TEST for {team['manager_name']}] {subject}"

        if args.dry_run:
            print(f"--- DRY RUN: would send to {destination} ---")
            print(subject)
            print(body[:500] + "...\n")
            continue

        try:
            send_email(gmail_address, gmail_app_password, destination, subject, body)
            print(f"Sent to {destination} ({team['team_name']})")
            sent += 1
        except Exception as e:
            print(f"FAILED to send to {destination} ({team['team_name']}): {e}")
            failed += 1

        time.sleep(2)  # gentle pacing, well within Gmail's limits for ~11 recipients

    print(f"\nDone. Sent: {sent}, Skipped (no email): {skipped}, Failed: {failed}")
    if failed:
        sys.exit(1)  # non-zero exit so the GitHub Actions step shows as failed


if __name__ == "__main__":
    main()
