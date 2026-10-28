# Operations and recovery

## Daily operation

On GitHub, use **Actions → X Manager → Run workflow**, branch **master**. The default manual mode is `dry-run`; select `live` deliberately when you want the program to publish eligible replies. `live` requires repository variable `X_AUTOMATION_ENABLED=true`.

| Mode | X activity | State/publishing purpose |
| --- | --- | --- |
| `check` | Account read | Verify credentials and expected username |
| `dry-run` | Bounded reads | Evaluate candidates; create no replies |
| `live` | Bounded reads and eligible reply creation | Persist attempts and outcomes on `bot-state` |
| `status` | No X requests | Read saved progress and unresolved outcomes |

There is no media upload, local always-on process, or GitHub photo archive involved. GitHub starts a runner for a pass and closes it afterward.

## Schedule and delays

The scheduled workflow has these five UTC entries:

| UTC cron | Local time at UTC+7 |
| --- | --- |
| `0 21 * * *` | 04:00 the following local calendar day |
| `0 1 * * *` | 08:00 |
| `0 5 * * *` | 12:00 |
| `0 9 * * *` | 16:00 |
| `0 13 * * *` | 20:00 |

These preserve the Instagram workflow's local times. GitHub schedules run from the default branch, and these operational jobs require `master`. Keep `master` as the default branch or update the workflow guards consistently if you rename it.

Cron is not an exact-time delivery service. GitHub documents delays and possible dropped queued runs during heavy load, particularly at the start of an hour. In public repositories, schedules are automatically disabled after 60 days without repository activity. Check the Actions page if runs stop and re-enable the workflow when appropriate. Do not assume state-branch updates guarantee continued scheduling. [GitHub schedule reference](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

The manager and recovery workflows share `x-manager-state` with `cancel-in-progress: false`. A new operation waits for the active one. GitHub's default concurrency queue can replace an older pending run with a newer pending one, so this is serialization, not a promise that every dispatched run will execute. [GitHub concurrency reference](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).

## Pause or disable

Set repository variable `X_AUTOMATION_ENABLED` to `false` or remove it. Future scheduled jobs are skipped, and manual live mode refuses to run. Read-only checks and recovery remain available.

That setting does not interrupt a job that already started with the previous environment. If an active run must stop immediately, cancel it from Actions, then inspect `status` before the next live run. Cancellation during an X create request may leave an uncertain outcome that needs reconciliation. Do not delete state to clear the block.

Disable `ENABLE_EMAIL_ALERTS` separately if you also want to stop future optional alerts.

## Keep the state branch

`state/engagement.json` on `bot-state` is the durable memory for processed interactions, opt-outs, scan progress, and pending outcomes. It is updated using the workflow's built-in `GITHUB_TOKEN`; the workflow needs repository contents-write permission. Preserve this branch when changing code, credentials, or schedules. `check`, `dry-run`, and `status` do not persist new interaction state or bootstrap a missing state branch.

Deleting the branch or resetting its history can cause previously handled interactions to become eligible again and can forget opt-outs. A local file from a diagnostic run is not a replacement for deployed GitHub state. Use the same backend for operational runs and recovery.

If the repository is public, its issues, Actions output, and committed state can disclose information publicly. Keep copied comments, user profiles, credentials, and raw API responses out of those locations. This project does not upload a log archive. Review diagnostics before sharing them, regardless of repository visibility.

## Resolve an uncertain reply

If an X request may have succeeded but the program could not confirm it, future publishing stops. A permanent create rejection can also leave a `blocked` record requiring review. Inspect the account's actual replies, the original incoming post, and the error before deciding what happened.

Open **Actions → Resolve Uncertain X Reply → Run workflow**, branch **master**. The form uses numeric public X Post IDs: the number following `/status/` in a post URL. Do not paste a complete URL, handle, or comment text.

The status summary identifies a pending item by a 16-character hash prefix and timestamp, rather than exposing its source ID. Find the corresponding incoming post on X. If you need to check whether a candidate source ID matches that prefix, this optional **PowerShell** command computes it locally without X or GitHub requests:

```powershell
Set-Location -LiteralPath 'C:\Users\halim\seo-yuna-x-manage'
.\.venv\Scripts\python.exe -c "import hashlib; post_id=input('Source post ID: ').strip(); print(hashlib.sha256(('post:' + post_id).encode('utf-8')).hexdigest()[:16])"
```

Enter the actual source ID in the workflow form after matching it. The form does not accept the hash prefix in place of the source ID.

### If the acknowledgement exists

1. Put the incoming interaction's numeric ID into `source_id`.
2. Put your existing acknowledgement's numeric ID into `reply_id`.
3. Leave `confirm_retry` false.
4. Run the workflow.

The program verifies that the supplied existing reply belongs to your authenticated account and directly replies to that exact source post. You must still visually confirm that it is the intended acknowledgement. This operation does not create a new reply.

### If you confirm no acknowledgement exists

1. Inspect the original incoming post and your account's replies around the attempt time.
2. Put the incoming post ID into `source_id`.
3. Leave `reply_id` empty.
4. Select `confirm_retry` only after you have confirmed that no acknowledgement was published.
5. Run the workflow.

This marks the interaction retryable; it does not send a reply immediately. A subsequent eligible live pass can handle it. If you cannot determine the outcome, leave it unresolved and continue investigating.

For a `blocked` record caused by an explicit rejection such as 400/403, fix the stated configuration/content/access issue before selecting retry. Do not use retry to repeatedly resubmit an unresolved rejection.

The recovery workflow remains usable while `X_AUTOMATION_ENABLED=false`. It uses the same state backend and concurrency group, and it disables email alerts. Its operator-provided post IDs may be present in GitHub workflow-run metadata; the hashed state format does not hide values deliberately entered into that form.

## Common failures

| Symptom | Action |
| --- | --- |
| Scheduled job is skipped | Check `X_AUTOMATION_ENABLED=true`, expected username, default branch, and whether GitHub disabled the schedule |
| Missing configuration | Add the exact secret/variable named by the workflow; keys belong under Secrets, switches and username under Variables |
| HTTP 401 | Verify all four OAuth 1.0a values; replace rotated or revoked values in both this repository and the local uploader |
| HTTP 403 | Read the stated restriction; check Read and write permissions, regenerate tokens after permission changes, and verify the interaction meets current reply rules |
| HTTP 402 | Check X API credits, billing access, and spending limits |
| HTTP 429 | Respect the reported reset/retry guidance; avoid repeated manual runs that consume the same quota |
| State save denied or conflict | Fix GitHub token/branch permissions or concurrent edits; the bot must save the attempt before publishing |
| State exceeds 1 MiB | Preserve `bot-state` and arrange a supported migration; deleting old records would also delete processing/opt-out history |
| Incomplete scan/page or request budget reached | Inspect volume and bounds; the cursor should not be advanced manually past unseen items |
| Uncertain reply blocks publishing | Use the recovery workflow after checking X; do not reset `bot-state` |
| No candidate is acknowledged | Check explicit own-mention entities, direct parent ownership, opt-outs, text filters, previous processing, and manual replies |
| Email alert fails | Verify the optional Gmail secrets and app password; check Actions even when mail delivery fails |

A successful `check` does not test write permission. A successful dry run does not create a reply. Verify the first actual eligible live interaction before relying on unattended operation.

## Updating actions and dependencies

Workflow actions are pinned to verified full commit SHAs. Their release versions and official sources are recorded in [Sources](SOURCES.md). Review a release before changing its SHA; do not replace a pin with a floating branch merely to clear an error.

Offline CI validates configuration, compiles the Python source, and runs `unittest` on Python 3.12 and 3.14. Installing Python/dependencies uses ordinary package-download network access, while the tests use mocks and do not call X or send email. CI does not receive the live repository secrets. Review CI results before enabling a changed live version.
