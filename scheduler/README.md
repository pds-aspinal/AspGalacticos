# Fixture-based gameweek updates

The dedicated scheduler Worker checks FPL every ten minutes. At 09:00
Europe/London on the calendar day after the last fixture assigned to a
gameweek, it dispatches the existing Python workflow. UK summer time is
handled automatically. A Sunday finish therefore means Monday at 9am;
a Thursday finish means Friday at 9am.

FPL must report every assigned fixture as finished and the event as
finished and data_checked. An undated fixture blocks the update. Fixtures
reassigned to a different FPL event are processed with that event. A
rearranged fixture that remains in the original event moves its recap.
The schedule uses the UK date of the final kickoff; Premier League games
normally finish on that same local calendar day.

If FPL is still finalizing at 9am, the Worker checks again every ten minutes
through that day. A failed GitHub run is retried, up to three total attempts.
An accepted dispatch is not considered a successful update: the next check
reads the actual run result. No exact start-time guarantee is possible while
GitHub supplies the runners. If FPL is still not ready by midnight, investigate
and dispatch manually when finalized; the Worker does not send old recaps on
unrelated days. Cloudflare invocation errors are recorded in Worker logs;
external failure notifications must be configured separately.

## Activation order

1. Run `migrations/20261110_update_delivery.sql` in the existing Supabase
   project's SQL Editor. The two new tables are private; the service role
   already used by GitHub can access them.
2. Publish the repository changes to `main`. The updated workflow removes
   GitHub's Tuesday cron and accepts explicit `gameweek` and `season` inputs.
   Do not disable the workflow itself: Cloudflare needs to dispatch it.
3. Create a fine-grained GitHub token for **only pds-aspinal/AspGalacticos**,
   with **Actions read and write** permission. Store it in the new Worker as
   the secret `GITHUB_TOKEN`; never put it in a committed file or chat.
4. Set `MIN_GAMEWEEK` in this folder's `wrangler.toml` to the next gameweek
   that has NOT already been emailed. The default `0` deliberately blocks
   activation to avoid duplicating an earlier manual update. Confirm the
   current season and baseline each season before re-enabling.
5. Deploy **this folder's** configuration, e.g. from the repository root:
   `npx wrangler deploy --config scheduler/wrangler.toml`.
   Add the secret with `npx wrangler secret put GITHUB_TOKEN --config scheduler/wrangler.toml`.
   This is a separate Worker; the existing Pages configuration is preserved.
6. Verify the new Cron Trigger, token and log output in Cloudflare. Configure
   failure notifications in your monitoring service, including an independent
   missed-success check if required. Merely logging Worker failures is not an
   alert. Run the tests below before enabling.

## Duplicate and partial-delivery protection

GitHub serializes all weekly update runs. Completed `(season, gameweek)`
updates are recorded in Supabase. Each manager's delivery is claimed before
SMTP sending and marked sent afterward, so retries skip confirmed deliveries.
If sending or recording success has an uncertain outcome, that delivery stays
`sending` and automatic resending stops. Inspect the Gmail sent folder and
the Actions log, then manually mark it `sent` if delivered, or remove only
that claim if confirmed unsent. SMTP does not provide an atomic transaction
with Supabase, so an uncertain outcome needs review to avoid duplicates.

The target gameweek is explicitly passed to emails and cups. A newer live
gameweek in the collected history cannot accidentally become the recap.
Missing team snapshots block processing rather than producing partial results.

## Manual recovery

Actions -> Collect FPL standings -> Run workflow: enter the completed
gameweek and season start year (2026 for 2026/27). The runner checks FPL
finalization and the 9am due time again. A completed update is a no-op.
Existing records sent before this migration are not automatically backfilled;
do not manually dispatch those weeks without first recording their delivery.

## Verification (no live email)

```
node scheduler/worker.test.mjs
python -m unittest discover -s tests
```

Tests cover UK winter/summer time, clock changes, a moved last fixture,
unfinished/undated fixtures, FPL finalization, successful/queued/failed runs,
retry limits, API failures, and email delivery claims.
