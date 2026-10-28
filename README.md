# Seo Yuna X Manager

A small Python manager for acknowledging eligible replies to your own X posts with one to three emojis. It runs on GitHub Actions, so your computer can be off. Image publishing stays in the separate local photo uploader; this repository does not store or upload your photos.

**Live automation is off until you configure the account and set `X_AUTOMATION_ENABLED=true`.** The X account, credentials, and live behavior still need to be verified. Offline tests do not establish that a particular X account has API access or permission to publish replies.

## What it does

- Scans incoming mentions within a bounded request/page budget.
- Selects direct replies to your own posts that are not themselves replies, and only when the incoming post contains an explicit mention entity for your username; incoming media/sensitive posts are skipped.
- Removes that one account's mention from the text being filtered, then applies the configured text, negative-term, question, other-mention, URL, and length filters.
- Skips recorded interactions and replies you have already answered manually.
- Publishes at most one fixed-pool emoji acknowledgement per eligible interaction, with a default ceiling of 10 replies per run.
- Records `STOP`, `UNSUBSCRIBE`, and `OPT OUT` requests and suppresses subsequent automated replies to those users.
- Writes durable state before each X create request and blocks further publishing when a request's outcome is uncertain.

Ordinary mentions, quote-only interactions, replies without the explicit account mention, unsolicited outreach, automated likes, and AI-generated replies are outside this implementation. X's [automation rules](https://help.x.com/en/rules-and-policies/x-automation) prohibit automated likes and place conditions on automatic replies. Its [reply API documentation](https://docs.x.com/x-api/posts/manage-tweets/introduction) describes additional self-serve reply restrictions.

## Start here

1. Follow [Setup](docs/SETUP.md) to reuse the same four OAuth 1.0a credentials as the photo uploader, add repository secrets, and set the expected username.
2. In **Actions → X Manager → Run workflow**, select `master` and run `check`, then `dry-run`.
3. Review [Behavior and limits](docs/BEHAVIOR.md), including opt-outs, the explicit mention requirement, and what a dry run can verify.
4. When ready for real replies, enable `X_AUTOMATION_ENABLED` and run a manual `live` pass. That variable also enables subsequent cron runs.
5. Use [Operations and recovery](docs/OPERATIONS.md) for pausing, inspecting state, and resolving an uncertain reply.

## Commands and workflows

| Command | Purpose |
| --- | --- |
| `python x_auto_engage.py validate` | Validate configuration and filter/reply data offline; no X or state access |
| `python x_auto_engage.py check` | Verify the authenticated account and expected username; no reply creation |
| `python x_auto_engage.py run --mode dry-run` | Read X and evaluate candidates without publishing replies |
| `python x_auto_engage.py run --mode live` | Publish eligible acknowledgements when the enable switch is true |
| `python x_auto_engage.py status` | Inspect saved state |
| `python x_auto_engage.py resolve --source-post-id ID --reply-post-id ID` | Verify an existing reply and reconcile an uncertain outcome |
| `python x_auto_engage.py resolve --source-post-id ID --retry` | Make an uncertain interaction retryable after you confirm no reply exists |

The **X Manager** workflow runs at 04:00, 08:00, 12:00, 16:00, and 20:00 in UTC+7 when enabled. These correspond to 21:00, 01:00, 05:00, 09:00, and 13:00 UTC. GitHub can delay or drop scheduled runs under load, and public-repository schedules can be disabled after 60 days without repository activity. See GitHub's [schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

The **Resolve Uncertain X Reply** workflow handles recovery without creating a reply. Both operational workflows share `x-manager-state` concurrency and do not cancel an active run. **Offline CI** tests Python 3.12 and 3.14 on pushes and pull requests to `master`; it receives no X or email credentials.

## State and costs

On GitHub Actions, state is saved as `state/engagement.json` on the `bot-state` branch through the repository Contents API using the built-in `GITHUB_TOKEN`. Per-interaction record keys and opted-out user identifiers are hashed; the scan cursor and your own account/reply IDs remain available as operational metadata. Raw incoming text, handles, media, and log archives are not committed. A live attempt logs its numeric source Post ID so you can use the recovery form if needed. If the repository is public, Actions output, state metadata, and commit timing are public too. Keep the state branch: deleting it removes duplicate and opt-out history.

Defaults are 10 replies with 15-second pacing, 60 X requests, and up to five mention pages of 100 items per run, with a 48-hour initial lookback. Own-reply checks are also bounded. These are workload limits, not a monetary spending cap. X read charges depend on returned resources, so even dry runs can consume credits. Use the [X Developer Console](https://console.x.com) to monitor credits and set your billing limits; see [X pricing](https://docs.x.com/x-api/getting-started/pricing).

## Repository scope

This is the X-specific adaptation of `budhi-halim/seo-yuna-ig-manage`. Its filter data and emoji pool are carried into the X version, while API calls, consent checks, state persistence, and workflows are specific to X. The Instagram repository and its workflows are separate.

Official references and the verified action pins are listed in [Sources](docs/SOURCES.md).
