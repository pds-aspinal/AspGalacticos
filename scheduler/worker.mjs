const FPL = "https://fantasy.premierleague.com/api";
const londonFormat = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Europe/London", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", hourCycle: "h23",
});

export function londonParts(date) {
  return Object.fromEntries(londonFormat.formatToParts(date).map(p => [p.type, p.value]));
}

function dateKey(parts) { return `${parts.year}-${parts.month}-${parts.day}`; }

export function dueGameweeks(bootstrap, fixtures, now, minimum) {
  const local = londonParts(now);
  if (Number(local.hour) < 9) return [];
  return bootstrap.events.filter(event => {
    if (event.id < minimum || !event.finished || !event.data_checked) return false;
    const matches = fixtures.filter(f => f.event === event.id);
    if (!matches.length || matches.some(f => !f.finished || !f.kickoff_time)) return false;
    const lastKickoff = new Date(Math.max(...matches.map(f => Date.parse(f.kickoff_time))));
    if (Number.isNaN(lastKickoff.valueOf())) return false;
    const lastDay = dateKey(londonParts(lastKickoff));
    const followingDay = new Date(`${lastDay}T12:00:00Z`);
    followingDay.setUTCDate(followingDay.getUTCDate() + 1);
    return dateKey(local) === followingDay.toISOString().slice(0, 10);
  }).map(event => ({
    gw: event.id,
    season: String(new Date(bootstrap.events[0].deadline_time).getUTCFullYear()),
  }));
}

async function json(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(`Request failed: ${response.status} ${new URL(url).pathname}`);
  return response.json();
}

export async function tick(env, now = new Date()) {
  const minimum = Number(env.MIN_GAMEWEEK);
  if (!Number.isInteger(minimum) || minimum < 1 || minimum > 38) {
    throw new Error("Set MIN_GAMEWEEK to the next unemailed gameweek (1-38) before enabling.");
  }
  if (Number(londonParts(now).hour) < 9) return;
  const [bootstrap, fixtures] = await Promise.all([
    json(`${FPL}/bootstrap-static/`, {headers: {"User-Agent": "Mozilla/5.0 (AspGalacticos scheduler)"}}),
    json(`${FPL}/fixtures/`, {headers: {"User-Agent": "Mozilla/5.0 (AspGalacticos scheduler)"}}),
  ]);
  for (const {gw, season} of dueGameweeks(bootstrap, fixtures, now, minimum)) {
    const title = `GW recap ${season} GW${gw}`;
    const base = `https://api.github.com/repos/${env.GITHUB_REPOSITORY}/actions/workflows/collect.yml`;
    const headers = {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`, "User-Agent": "AspGalacticos scheduler",
      Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
    };
    // Check actual completion, rather than treating an accepted dispatch as success.
    // Fail closed on API errors: never dispatch blindly when status is unavailable.
    const since = new Date(now.valueOf() - 48 * 60 * 60 * 1000).toISOString();
    const params = new URLSearchParams({event: "workflow_dispatch", branch: env.GITHUB_REF,
      created: `>=${since}`, per_page: "100"});
    const {workflow_runs: runs} = await json(`${base}/runs?${params}`, {headers});
    const matching = runs.filter(r => r.display_title === title);
    if (matching.some(r => r.conclusion === "success" || r.status !== "completed")) {
      console.log(`${title}: already successful or running`);
      continue;
    }
    if (matching.length >= 3) {
      throw new Error(`${title}: three failed attempts; inspect GitHub Actions before retrying manually`);
    }
    const response = await fetch(`${base}/dispatches`, {
      method: "POST", headers: {...headers, "Content-Type": "application/json"},
      body: JSON.stringify({ref: env.GITHUB_REF, inputs: {gameweek: String(gw), season}}),
    });
    if (!response.ok) throw new Error(`Dispatch failed: ${response.status}`);
    console.log(`${title}: dispatched`);
  }
}

export default {
  async scheduled(_controller, env, ctx) {
    ctx.waitUntil(tick(env));
  },
};
