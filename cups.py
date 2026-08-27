"""
Cup simulation engine — pure functions, no network/database calls.

These operate on plain data structures so they can be unit tested with
synthetic data before ever touching real scores. The Supabase-connected
driver (cups.py) wraps these with actual data fetching/writing.
"""

import hashlib


class TiebreakNeeded(Exception):
    """Raised when a genuine tie can't be resolved from data already on hand
    (points alone) and needs goals-scored/conceded data to break it."""
    def __init__(self, tied_team_ids, gw):
        self.tied_team_ids = tied_team_ids
        self.gw = gw
        super().__init__(f"Tie at GW{gw} among {tied_team_ids} needs goals data")


def resolve_tiebreak(tied_team_ids, goals_scored, goals_conceded, cup_name, gw):
    """
    Resolve a tie among tied_team_ids using, in order:
      1. Fewest goals scored (that's who goes out, in an elimination context)
      2. Most goals conceded (among those still tied)
      3. A deterministic "virtual coin toss" (reproducible — re-running this
         function with the same inputs always gives the same answer, so a
         script re-run never silently redraws a decided tie)

    goals_scored / goals_conceded: {team_id: int}

    Returns (team_id_to_eliminate, description_of_which_rule_applied)
    """
    pool = list(tied_team_ids)

    min_scored = min(goals_scored[t] for t in pool)
    pool_after_goals = [t for t in pool if goals_scored[t] == min_scored]
    if len(pool_after_goals) == 1:
        return pool_after_goals[0], "goals scored"

    max_conceded = max(goals_conceded[t] for t in pool_after_goals)
    pool_after_conceded = [t for t in pool_after_goals if goals_conceded[t] == max_conceded]
    if len(pool_after_conceded) == 1:
        return pool_after_conceded[0], "goals conceded"

    # Deterministic "coin toss": hash the cup/gw/sorted-team-ids together so
    # the same tie always resolves the same way, but it's not predictable
    # in advance without knowing the hash.
    key = f"{cup_name}|{gw}|{'-'.join(str(t) for t in sorted(pool_after_conceded))}"
    digest = hashlib.sha256(key.encode()).hexdigest()
    chosen_index = int(digest, 16) % len(pool_after_conceded)
    return pool_after_conceded[chosen_index], "coin toss"


def resolve_head_to_head_tiebreak(team1_id, team2_id, goals_scored, goals_conceded, cup_name, gw):
    """Same tiebreak chain, but for a head-to-head match (returns the WINNER, not who's eliminated)."""
    loser, rule = resolve_tiebreak([team1_id, team2_id], goals_scored, goals_conceded, cup_name, gw)
    winner = team2_id if loser == team1_id else team1_id
    return winner, rule


def next_elimination_round(cup_name, active_team_ids, gw, scores, get_goals_fn):
    """
    Process ONE elimination round (one gameweek) for an already-active pool
    of teams. Returns (eliminated_team_id, score, tiebreak_description_or_None).

    scores: {team_id: points} for this specific gw (only active teams need be present)
    get_goals_fn: callable(team_ids, gw) -> (goals_scored_dict, goals_conceded_dict),
                  only called if a tiebreak is actually needed (lazy fetch)
    """
    present = {t: scores[t] for t in active_team_ids if t in scores}
    if not present:
        return None, None, None  # no scores yet for this gw — nothing to process

    lowest = min(present.values())
    tied = [t for t, pts in present.items() if pts == lowest]

    if len(tied) == 1:
        return tied[0], lowest, None

    goals_scored, goals_conceded = get_goals_fn(tied, gw)
    eliminated, rule = resolve_tiebreak(tied, goals_scored, goals_conceded, cup_name, gw)
    return eliminated, lowest, rule


def simulate_elimination_cup(cup_name, all_team_ids, start_gw, latest_available_gw,
                              scores, already_eliminated, get_goals_fn,
                              stop_at_n_remaining=1):
    """
    Simulate every not-yet-processed round of a pure elimination cup, from
    wherever it left off, up to the latest gameweek we have real data for.

    all_team_ids: every team that started in this cup
    scores: {(team_id, gw): points}
    already_eliminated: {team_id: (gw, score, tiebreak)} — already recorded, don't redo
    stop_at_n_remaining: stop simulating once this many teams are still active
                         (1 for Cup 1's outright winner; 4 for Cup 2, which
                         then hands off to the semi/final head-to-head stage)

    Returns: (new_eliminations, active_team_ids_remaining, winner_or_None)
      new_eliminations: list of dicts ready to insert into cup_eliminations
    """
    active = [t for t in all_team_ids if t not in already_eliminated]
    new_eliminations = []

    gw = start_gw
    # Fast-forward past gameweeks already processed (based on max gw in already_eliminated)
    processed_gws = {v[0] for v in already_eliminated.values()}
    if processed_gws:
        gw = max(processed_gws) + 1

    while gw <= latest_available_gw and len(active) > stop_at_n_remaining:
        gw_scores = {t: scores[(t, gw)] for t in active if (t, gw) in scores}
        eliminated, score, tiebreak = next_elimination_round(cup_name, active, gw, gw_scores, get_goals_fn)
        if eliminated is None:
            gw += 1
            continue  # no data for this gw yet (e.g. gameweek not finished) — try next time
        new_eliminations.append({
            "cup_name": cup_name, "gw": gw, "team_id": eliminated,
            "score": score, "tiebreak_used": tiebreak,
        })
        active.remove(eliminated)
        gw += 1

    winner = active[0] if len(active) == 1 and stop_at_n_remaining == 1 else None
    return new_eliminations, active, winner


def draw_random_pairs(team_ids, cup_name, salt=""):
    """
    Randomly pair up teams (expects exactly 4, but works for any even count).
    Uses os.urandom-backed randomness — a genuine draw, not deterministic —
    intended to be called exactly ONCE per cup and immediately persisted so
    it's never redrawn on a subsequent run.
    """
    import random
    shuffled = list(team_ids)
    random.SystemRandom().shuffle(shuffled)
    pairs = [(shuffled[i], shuffled[i + 1]) for i in range(0, len(shuffled), 2)]
    return pairs


def resolve_head_to_head_match(team1_id, team2_id, gw, scores, get_goals_fn, cup_name, round_label):
    """
    Resolve a single head-to-head match for a given gameweek.
    Returns a dict ready for cup_matches, or None if scores aren't in yet.
    """
    if (team1_id, gw) not in scores or (team2_id, gw) not in scores:
        return None
    s1, s2 = scores[(team1_id, gw)], scores[(team2_id, gw)]
    tiebreak = None
    if s1 > s2:
        winner = team1_id
    elif s2 > s1:
        winner = team2_id
    else:
        winner, tiebreak = resolve_head_to_head_tiebreak(team1_id, team2_id, *get_goals_fn([team1_id, team2_id], gw), cup_name, gw)
    return {
        "cup_name": cup_name, "round_label": round_label, "gw": gw,
        "team1_id": team1_id, "team2_id": team2_id,
        "team1_score": s1, "team2_score": s2,
        "winner_team_id": winner, "tiebreak_used": tiebreak,
    }
