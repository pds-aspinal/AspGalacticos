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
  SUPABASE_SERVICE_KEY         service role key (NOT anon key - needs write-level read access)
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

# Approximate FPL's own difficulty colour scale (1 = easiest/green, 5 = hardest/red)
FDR_COLORS = {
    1: ("#1a7a4c", "#ffffff"),
    2: ("#4cbf6c", "#ffffff"),
    3: ("#e0e0e0", "#333333"),
    4: ("#e8615c", "#ffffff"),
    5: ("#b71c3c", "#ffffff"),
}

BRAND_DARK = "#1b3a6b"
TEXT_DARK = "#222222"
TEXT_MUTED = "#767676"
BORDER = "#ededed"
# Outlook desktop renders HTML email with Word's engine, which adds its own
# spacing around tables unless explicitly told not to.
TABLE_RESET = "border-collapse:collapse;mso-table-lspace:0pt;mso-table-rspace:0pt;"


def fdr_cell_style(difficulty):
    """Style string for a <td> acting as a colour badge - td background-color
    and padding are honoured far more reliably across Outlook clients than
    the same properties on a <span>."""
    bg, fg = FDR_COLORS.get(difficulty, ("#e0e0e0", "#333333"))
    return (
        f'background-color:{bg};color:{fg};font-size:10px;font-weight:bold;'
        f'padding:3px 0;border-radius:4px;text-align:center;white-space:nowrap;'
    )


def pill_table(text, bg, fg):
    """A single-cell table used as a coloured 'pill' - more reliable than a
    styled span across Outlook clients."""
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" style="{TABLE_RESET}">'
        f'<tr><td style="background-color:{bg};color:{fg};font-size:11px;font-weight:bold;'
        f'padding:4px 10px;border-radius:12px;white-space:nowrap;">{text}</td></tr></table>'
    )


def rank_delta_badge(rank_now, rank_before):
    if rank_before is None:
        return '<span style="font-size:11px;color:' + TEXT_MUTED + ';">First tracked GW</span>'
    diff = rank_before - rank_now
    if diff > 0:
        return f'<span style="font-size:12px;color:#1a7a4c;font-weight:bold;">\u25b2 up {diff}</span>'
    if diff < 0:
        return f'<span style="font-size:12px;color:#b71c3c;font-weight:bold;">\u25bc down {abs(diff)}</span>'
    return f'<span style="font-size:12px;color:{TEXT_MUTED};">\u2014 no change</span>'


def stat_card(value, label, sublabel_html=""):
    return f"""
    <td width="33%" align="center" valign="top" style="background:#f4f7fb;border-radius:8px;padding:14px 6px;">
      <div style="font-size:24px;font-weight:bold;color:{BRAND_DARK};line-height:1.1;">{value}</div>
      <div style="margin-top:2px;">{sublabel_html}</div>
      <div style="font-size:10px;color:{TEXT_MUTED};text-transform:uppercase;letter-spacing:.4px;margin-top:4px;">{label}</div>
    </td>
    """


def build_recap_section(team_id, teams, snap_now, snap_prev, all_snaps_now):
    team = teams[team_id]
    row = snap_now.get(team_id)
    if row is None:
        return f'<p style="color:{TEXT_MUTED};">No snapshot found for {team["team_name"]} this GW.</p>'

    prev_row = snap_prev.get(team_id) if snap_prev else None
    rank_badge = rank_delta_badge(row["rank"], prev_row["rank"] if prev_row else None)

    gw_points_all = [r["gw_points"] for r in all_snaps_now.values()]
    weekly_best = max(gw_points_all)
    weekly_worst = min(gw_points_all)

    stats_row = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="{TABLE_RESET}margin-bottom:14px;">'
        '<tr>'
        + stat_card(row["gw_points"], "GW Points")
        + '<td width="10" style="font-size:0;line-height:0;">&nbsp;</td>'
        + stat_card(f'#{row["rank"]}', "League Rank", rank_badge)
        + '<td width="10" style="font-size:0;line-height:0;">&nbsp;</td>'
        + stat_card(row["total_points"], "Total Points")
        + "</tr></table>"
    )

    pills = []
    if row.get("chip"):
        pills.append(pill_table(f'{CHIP_NAMES.get(row["chip"], row["chip"])} played', "#eef2fb", BRAND_DARK))
    if row.get("transfer_cost"):
        pills.append(pill_table(f'-{row["transfer_cost"]} pts transfer hit', "#fdeeee", "#b71c3c"))

    if pills:
        cells = "".join(f'<td style="padding-right:8px;">{p}</td>' for p in pills)
        badges_html = (
            f'<table role="presentation" cellpadding="0" cellspacing="0" '
            f'style="{TABLE_RESET}margin-bottom:14px;"><tr>{cells}<td></td></tr></table>'
        )
    else:
        badges_html = ""

    if row["gw_points"] == weekly_best:
        banner_bg, banner_text = "#eafaf0", f'\U0001F3C6 <strong>Highest score in the league</strong> this GW!'
    elif row["gw_points"] == weekly_worst:
        banner_bg, banner_text = "#fdeeee", f"Rough week \u2014 the league's high score was {weekly_best}."
    else:
        banner_bg, banner_text = "#f4f7fb", f"League this GW ranged from {weekly_worst} to {weekly_best} points."

    banner_html = (
        f'<div style="background:{banner_bg};border-radius:6px;padding:10px 14px;'
        f'font-size:13px;color:{TEXT_DARK};">{banner_text}</div>'
    )

    return stats_row + badges_html + banner_html


def fixture_row_table(f, teams_by_fpl_id, show_border):
    h = teams_by_fpl_id[f["team_h"]]["short_name"]
    a = teams_by_fpl_id[f["team_a"]]["short_name"]
    dh, da = f["team_h_difficulty"], f["team_a_difficulty"]
    border = f'border-bottom:1px solid {BORDER};' if show_border else ''
    return f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="{TABLE_RESET}">
      <tr>
        <td style="font-size:12px;color:{TEXT_DARK};padding:6px 0;{border}">{h}</td>
        <td width="20" align="center" style="{fdr_cell_style(dh)}">{dh}</td>
        <td width="26" align="center" style="font-size:11px;color:{TEXT_MUTED};padding:6px 2px;{border}">vs</td>
        <td style="font-size:12px;color:{TEXT_DARK};padding:6px 0;{border}">{a}</td>
        <td width="20" align="center" style="{fdr_cell_style(da)}">{da}</td>
      </tr>
    </table>
    """


def fixture_column(title, fixtures, teams_by_fpl_id, header_bg):
    rows = "".join(
        fixture_row_table(f, teams_by_fpl_id, show_border=(i < len(fixtures) - 1))
        for i, f in enumerate(fixtures)
    )
    return f"""
    <td width="48%" valign="top" style="background:#fafafa;border-radius:8px;padding:12px 14px;">
      <div style="display:inline-block;background:{header_bg};color:#ffffff;font-size:11px;font-weight:bold;
                  text-transform:uppercase;letter-spacing:.4px;padding:3px 8px;border-radius:4px;margin-bottom:8px;">
        {title}
      </div>
      {rows}
    </td>
    """


def build_general_fdr_section(fixtures, teams_by_fpl_id, next_gw_number):
    if not fixtures:
        return (
            f'<p style="color:{TEXT_MUTED};font-size:13px;">'
            f'No fixtures found for GW{next_gw_number} yet (blank gameweek or fixtures not released).</p>'
        )

    sorted_fixtures = sorted(fixtures, key=lambda f: f["team_h_difficulty"] + f["team_a_difficulty"])
    easiest = sorted_fixtures[:3]
    hardest = sorted_fixtures[-3:]

    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="{TABLE_RESET}margin-bottom:18px;"><tr>'
        + fixture_column("Easiest fixtures", easiest, teams_by_fpl_id, "#1a7a4c")
        + '<td width="16" style="font-size:0;line-height:0;">&nbsp;</td>'
        + fixture_column("Toughest fixtures", hardest, teams_by_fpl_id, "#b71c3c")
        + "</tr></table>"
    )


def build_squad_fixture_section(team_id, next_gw_number, bootstrap, fixtures_by_team):
    picks_data = get_entry_picks(team_id, next_gw_number - 1)
    # NOTE: FPL's picks endpoint returns the squad picked FOR that gw; the most
    # recently completed gw's picks are the best available proxy for "your
    # current squad" until the next gw's picks are locked in.
    if not picks_data:
        return f'<p style="color:{TEXT_MUTED};font-size:13px;">Couldn\'t retrieve your squad from the FPL API for a fixture breakdown this week.</p>'

    elements_by_id = {e["id"]: e for e in bootstrap["elements"]}
    teams_by_id = {t["id"]: t for t in bootstrap["teams"]}

    starters = [p for p in picks_data.get("picks", []) if p.get("multiplier", 0) > 0]
    if not starters:
        return f'<p style="color:{TEXT_MUTED};font-size:13px;">No starting XI data available for a fixture breakdown this week.</p>'

    rows = []
    for p in starters:
        el = elements_by_id.get(p["element"])
        if not el:
            continue
        team_short = teams_by_id[el["team"]]["short_name"]
        fixtures = fixtures_by_team.get(el["team"], [])
        if not fixtures:
            chips_html = f'<span style="color:{TEXT_MUTED};font-size:12px;">No fixture (blank)</span>'
        else:
            parts = []
            for f in fixtures:
                is_home = f["team_h"] == el["team"]
                opp_id = f["team_a"] if is_home else f["team_h"]
                opp_short = teams_by_id[opp_id]["short_name"]
                difficulty = f["team_h_difficulty"] if is_home else f["team_a_difficulty"]
                venue = "H" if is_home else "A"
                # A small standalone table per fixture, right-aligned with the HTML
                # align attribute (works in Outlook where CSS margin:auto often doesn't).
                # Multiple fixtures (double gameweeks) stack, one per line.
                parts.append(
                    f'<table align="right" role="presentation" cellpadding="0" cellspacing="0" '
                    f'style="{TABLE_RESET}margin-bottom:2px;">'
                    f'<tr>'
                    f'<td style="font-size:12px;color:{TEXT_DARK};padding:0 6px 0 0;white-space:nowrap;">{opp_short} ({venue})</td>'
                    f'<td width="20" align="center" style="{fdr_cell_style(difficulty)}">{difficulty}</td>'
                    f'</tr></table>'
                )
            chips_html = "".join(parts)
        rows.append(
            f'<tr>'
            f'<td style="padding:8px 0;border-bottom:1px solid {BORDER};font-size:13px;color:{TEXT_DARK};'
            f'mso-line-height-rule:exactly;line-height:20px;">'
            f'<strong>{el["web_name"]}</strong> <span style="color:{TEXT_MUTED};font-size:11px;">({team_short})</span>'
            f'</td>'
            f'<td style="padding:8px 0;border-bottom:1px solid {BORDER};text-align:right;">{chips_html}</td>'
            f'</tr>'
        )

    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="{TABLE_RESET}">'
        + "".join(rows) + "</table>"
    )


def build_email_html(manager_name, team_name, recap_html, fdr_html, squad_html, current_gw, next_gw):
    return f"""\
<html>
  <head>
    <!--[if mso]>
    <style type="text/css">
      table {{border-collapse:collapse;}}
      td {{mso-line-height-rule:exactly;}}
    </style>
    <![endif]-->
  </head>
  <body style="margin:0;padding:0;background-color:#f4f4f4;font-family:Arial, Helvetica, sans-serif;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="{TABLE_RESET}background-color:#f4f4f4;padding:24px 0;">
      <tr>
        <td align="center">
          <table role="presentation" width="600" cellpadding="0" cellspacing="0"
                 style="{TABLE_RESET}background-color:#ffffff;border-radius:10px;overflow:hidden;max-width:600px;">
            <tr>
              <td style="background-color:{BRAND_DARK};padding:22px 32px;">
                <div style="color:#ffffff;font-size:19px;font-weight:bold;">Aspinal Galacticos</div>
                <div style="color:#c9d6ea;font-size:13px;margin-top:2px;">GW{current_gw} Update &middot; {team_name}</div>
              </td>
            </tr>
            <tr>
              <td style="padding:26px 32px;">
                <p style="font-size:14px;color:{TEXT_DARK};margin:0 0 18px 0;">
                  Hi {manager_name}, here's how <strong>{team_name}</strong> got on in GW{current_gw},
                  and what's coming up in GW{next_gw}.
                </p>

                <h3 style="font-size:14px;color:{TEXT_DARK};text-transform:uppercase;letter-spacing:.4px;
                           border-bottom:2px solid {BRAND_DARK};padding-bottom:6px;margin:0 0 14px 0;">
                  GW{current_gw} Recap
                </h3>
                {recap_html}

                <h3 style="font-size:14px;color:{TEXT_DARK};text-transform:uppercase;letter-spacing:.4px;
                           border-bottom:2px solid {BRAND_DARK};padding-bottom:6px;margin:24px 0 14px 0;">
                  GW{next_gw} Preview
                </h3>
                {fdr_html}
                {squad_html}
              </td>
            </tr>
            <tr>
              <td style="padding:16px 32px;background-color:#fafafa;border-top:1px solid {BORDER};">
                <a href="{DASHBOARD_URL}" style="color:{BRAND_DARK};font-size:13px;font-weight:bold;text-decoration:none;">
                  View the full league dashboard &rarr;
                </a>
                <div style="color:#999999;font-size:11px;margin-top:8px;">
                  Automated update from the Aspinal Galacticos League tracker.
                </div>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
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
    service_key = os.environ["SUPABASE_SERVICE_KEY"]
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
