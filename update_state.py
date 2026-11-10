"""Private delivery state, accessible only through the Supabase service role."""
import os
import requests


def state_request(method, table, *, params=None, row=None):
    key = os.environ["SUPABASE_SERVICE_KEY"]
    response = requests.request(
        method, f"{os.environ['SUPABASE_URL'].rstrip('/')}/rest/v1/{table}",
        headers={"apikey": key, "Authorization": f"Bearer {key}",
                 "Prefer": "return=representation"},
        params=params, json=row, timeout=30,
    )
    response.raise_for_status()
    return response.json() if response.content else []


def completed(season, gw):
    return bool(state_request("GET", "gw_update_runs", params={
        "season": f"eq.{season}", "gw": f"eq.{gw}", "select": "gw",
    }))


def claim_email(season, gw, team_id):
    params = {"season": f"eq.{season}", "gw": f"eq.{gw}", "team_id": f"eq.{team_id}"}
    rows = state_request("GET", "gw_email_deliveries", params=params)
    if rows:
        if rows[0]["status"] == "sent":
            return False
        raise RuntimeError(f"GW{gw} team {team_id}: previous email delivery is uncertain; inspect before retrying")
    state_request("POST", "gw_email_deliveries", row={
        "season": season, "gw": gw, "team_id": team_id, "status": "sending",
    })
    return True


def mark_email_sent(season, gw, team_id):
    state_request("PATCH", "gw_email_deliveries", params={
        "season": f"eq.{season}", "gw": f"eq.{gw}", "team_id": f"eq.{team_id}",
    }, row={"status": "sent"})
