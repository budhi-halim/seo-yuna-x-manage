# Setup

The target repository is [budhi-halim/seo-yuna-x-manage](https://github.com/budhi-halim/seo-yuna-x-manage). Configure it only after the intended X account exists. You can install and run the offline tests beforehand; leave live automation disabled while account setup and API access are unfinished.

## 1. Reuse the uploader's X credentials

You need the same four OAuth 1.0a values used by the local photo uploader:

| Repository secret | Photo uploader field | Credential in X's console |
| --- | --- | --- |
| `X_API_KEY` | `api_key` | API Key / Consumer Key |
| `X_API_KEY_SECRET` | `api_key_secret` | API Key Secret / Consumer Secret |
| `X_ACCESS_TOKEN` | `access_token` | Your account's Access Token |
| `X_ACCESS_TOKEN_SECRET` | `access_token_secret` | Your account's Access Token Secret |

A second set of X keys is not required. If you have not created the app yet, open [console.x.com](https://console.x.com), sign in with the intended creator account, finish developer registration, and create an app with an accurate description of your own-account publishing and reply automation.

Set its OAuth 1.0a permissions to **Read and write**, save, and then generate the account's access token and token secret. If you generated a token while permissions were read-only, regenerate the token pair after changing the permissions. Do not substitute the app-only Bearer Token or OAuth 2.0 Client ID/Secret for these values. X documents [own-account token generation](https://docs.x.com/fundamentals/authentication/oauth-1-0a/overview) and [app permissions](https://docs.x.com/fundamentals/developer-apps).

If the console requires a callback URL while configuring authentication, `http://127.0.0.1/callback` is a local-development value. Neither this manager nor the local uploader visits it when using pre-generated own-account tokens. If a website field appears, use an appropriate real URL for the app or creator presence.

The two programs share the same app/account limits and billing resources when they use the same credentials. Running both can consume the same available credits and quota. Rotating or revoking a shared key/token affects both programs: update the GitHub secrets and the local uploader's private configuration together. Review current credits, access, and spending settings in the developer console; an X subscription does not configure this repository's API access.

## 2. Add four repository secrets

In the X repository:

1. Open **Settings → Secrets and variables → Actions**.
2. Select the **Secrets** tab and click **New repository secret**.
3. Enter one exact name from the table above, paste its corresponding value, and click **Add secret**.
4. Repeat for all four secrets.

Use repository secrets, not repository variables, for credentials. GitHub does not show a saved secret's value again. If you need to reuse values from another repository, copy them from your own secure record; the old repository's Secrets page cannot reveal them. [GitHub documents this setup flow here.](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)

Do not add a separate `GITHUB_TOKEN` secret. GitHub provides that token to each workflow run. The operational workflows request `contents: write` so the program can save the `bot-state` branch. Their checkout step does not persist credentials in the working copy.

If organization/repository policy blocks the requested token permission, allow the workflow's repository-content writes under **Settings → Actions → General**, or ask the repository administrator to permit them. The bot must be able to durably save an attempted reply before it publishes; a failed state write stops publishing.

## 3. Set the repository variables

Open **Settings → Secrets and variables → Actions → Variables → New repository variable**. Add the following values:

| Variable | Value during setup | Purpose |
| --- | --- | --- |
| `X_EXPECTED_USERNAME` | Your intended X username, without `@` | Required account guard before X operations |
| `X_AUTOMATION_ENABLED` | Leave unset, or use `false` | Keeps scheduled jobs and manual live mode disabled |
| `ENABLE_EMAIL_ALERTS` | Leave unset, or use `false` | Keeps optional Gmail alerts disabled |

The enable value is the lowercase string `true` when you later choose to enable live operation. It is not needed for `check`, `dry-run`, or `status`. A missing expected username or X credential stops the relevant workflow before any X request. The status mode can inspect repository state without X credentials.

GitHub's [repository-variable instructions](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-variables#creating-configuration-variables-for-a-repository) show the same UI flow. Variables are ordinary configuration, not secret storage.

## 4. Review the filters and reply behavior

Review `config.json`, `filters.txt`, and `replies.txt` before enabling live mode. The default reply ceiling is 10 per run with 15 seconds between acknowledgements, the X request budget is 60, and the initial lookback is 48 hours. Mention pagination defaults to five pages of 100 posts; each own-reply scan defaults to at most 10 pages. [Behavior and limits](BEHAVIOR.md#configuration) lists every configuration field.

The sender must directly reply to one of your posts that is not itself a reply, and explicitly include your username as a mention entity. Replies to your own replies are excluded to avoid acknowledgement loops. Incoming attachments or posts marked sensitive are also excluded. The manager removes only your own mention when applying the text filters. Mentions of other people, question-like text, links, long messages, and configured excluded terms still cause a skip. It acknowledges using the fixed emoji pool; it does not generate an answer to a question.

Make the behavior and opt-out available to people who interact with the account. For example, your profile or a pinned explanation can state that eligible replies explicitly mentioning the account may receive an automated emoji acknowledgement, and that mentioning the account with `STOP`, `UNSUBSCRIBE`, or `OPT OUT` opts out. This program does not edit your profile or publish that notice.

Read [Behavior and limits](BEHAVIOR.md) for the exact scope. Eligibility checks implement the intended workflow; they are not a platform approval or a guarantee that X will accept every request.

## 5. Run account check and dry run

Open the repository's **Actions** tab. If Actions needs enabling, enable it for this repository. Select **X Manager**, then **Run workflow**:

1. Select branch **master**.
2. Choose mode **check** and start the workflow.
3. Open the run and confirm that account verification succeeds for `X_EXPECTED_USERNAME`.
4. Run the same workflow with mode **dry-run**.
5. Review the summary and skip counts before deciding to enable replies.

`check` and `dry-run` make read requests to X and can consume API credits. They do not create replies, send email, or persist new processed/opt-out/cursor state. A successful account check proves that read authentication works; it does not prove that the account can publish a reply to a particular incoming post. A dry run with no eligible interactions cannot establish that the full live path works.

If the new account has no posts or interactions yet, an empty dry run is expected. Do not change the filters merely to force a live action. A later genuine direct reply that explicitly mentions the account can exercise the intended path.

## 6. Enable live operation when ready

After reviewing the dry run and the live behavior:

1. Set repository variable `X_AUTOMATION_ENABLED` to `true`.
2. Run **X Manager** manually with mode **live** on **master**.
3. Inspect any resulting acknowledgements on X and the saved status before leaving it unattended.

The manual live run can publish up to the configured reply limit. Changing the variable to `true` also enables cron; it is not a manual-only switch. Set it back to `false` to stop new scheduled/live runs. If a run is already active, follow the pause guidance in [Operations](OPERATIONS.md).

No live X account test has been performed as part of preparing this repository. Account creation, API credentials, credits, a genuine eligible interaction, and your first observed live reply remain setup work. The separate local photo uploader also needs its own account check and first-image live verification.

## 7. Optional Gmail alerts

This section is optional. Leave `ENABLE_EMAIL_ALERTS` unset/`false` if you do not want email.

To reuse the existing Instagram manager's email configuration, add the same privately retained values as repository secrets here:

| Secret | Value |
| --- | --- |
| `GMAIL_ADDRESS` | Gmail sender address |
| `GMAIL_APP_PASSWORD` | App password for that sender account |
| `NOTIFICATION_EMAIL` | Address where you want alerts delivered |

Then set repository variable `ENABLE_EMAIL_ALERTS=true`. The live workflow checks that all three email secrets are present when alerts are enabled. Alerts apply to live-run X errors 401, 402, and 403; they are throttled to at most one attempt per 24 hours using state saved before the SMTP attempt. Secrets from the Instagram repository are not automatically available in this repository. Read-only modes do not send alerts.

**Only if you need a new Gmail app password:** enable two-step verification on the sender's Google account, open [App passwords](https://myaccount.google.com/apppasswords), create one for this manager, and save it directly as `GMAIL_APP_PASSWORD`. Use an app password, not the normal account password. Some managed accounts, security-key-only configurations, and Advanced Protection accounts do not offer this option. Google also revokes app passwords when the account password changes. [Google's app-password guidance](https://support.google.com/accounts/answer/185833) explains eligibility and recovery.

This setup does not send a test email. The optional alert path should be verified separately when you deliberately enable it. The recovery workflow and offline CI disable email alerts.

## 8. Optional local setup on Windows

Save or clone the repository to exactly:

`C:\Users\halim\seo-yuna-x-manage`

If using Git for the first time with this folder, run this in **PowerShell**:

```powershell
Set-Location -LiteralPath 'C:\Users\halim'
git clone https://github.com/budhi-halim/seo-yuna-x-manage.git 'C:\Users\halim\seo-yuna-x-manage'
```

If you downloaded a ZIP instead, extract it so `x_auto_engage.py` is directly inside that exact folder. Install the Python environment:

```powershell
Set-Location -LiteralPath 'C:\Users\halim\seo-yuna-x-manage'
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Run the offline checks without credentials:

```powershell
Set-Location -LiteralPath 'C:\Users\halim\seo-yuna-x-manage'
$env:X_AUTOMATION_ENABLED = 'false'
$env:ENABLE_EMAIL_ALERTS = 'false'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q x_auto_engage.py xmanage tests
.\.venv\Scripts\python.exe .\x_auto_engage.py validate
```

For local read-only account checking, enter the four credentials at hidden prompts in the same PowerShell window. The values are kept in that process environment; the commands themselves do not contain them:

```powershell
Set-Location -LiteralPath 'C:\Users\halim\seo-yuna-x-manage'
foreach ($credentialName in 'X_API_KEY', 'X_API_KEY_SECRET', 'X_ACCESS_TOKEN', 'X_ACCESS_TOKEN_SECRET') {
    $secureCredential = Read-Host $credentialName -AsSecureString
    $plainCredential = [System.Net.NetworkCredential]::new('', $secureCredential).Password
    [Environment]::SetEnvironmentVariable($credentialName, $plainCredential, 'Process')
}
$env:X_EXPECTED_USERNAME = Read-Host 'Expected X username, without @'
$env:X_AUTOMATION_ENABLED = 'false'
$env:ENABLE_EMAIL_ALERTS = 'false'
$env:STATE_BACKEND = 'local'
.\.venv\Scripts\python.exe .\x_auto_engage.py check
```

Then run a local dry run, still in that PowerShell session:

```powershell
Set-Location -LiteralPath 'C:\Users\halim\seo-yuna-x-manage'
.\.venv\Scripts\python.exe .\x_auto_engage.py run --mode dry-run
```

The local backend uses `state/local_state.json`, separate from the deployed GitHub state. Read-only diagnostics do not create new processing history. Use the GitHub workflows for live operation and recovery so both use the same durable history and concurrency group. Do not run an independent local live instance against the account while GitHub automation is active. Close the PowerShell window after diagnostics to discard its process environment.
