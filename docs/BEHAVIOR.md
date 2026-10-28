# Behavior and limits

## The X adaptation

The Instagram manager scans your own comments, filters them, skips processed/manual answers, likes eligible comments, and selects one to three emojis for a reply. This X manager retains the filter/reply data and the five daily UTC+7 times, while implementing X's different interaction and API rules.

| Instagram behavior | X implementation |
| --- | --- |
| Scan comments on owned media | Read bounded incoming mentions; verify a direct reply's parent belongs to the authenticated account |
| Like a qualifying comment | Omitted: X prohibits automated likes |
| Send a short emoji reply | One fixed-pool acknowledgement, only for an eligible explicitly addressed direct reply |
| Skip processed comments | Persistent hashed interaction history on `bot-state` |
| Skip a comment you already answered | Check the account's own replies before publishing |
| Ignore text containing `@` | Remove the authenticated account's own mention first; other mentions still fail the filter |
| Authentication email alerts | Optional Gmail settings with a separate enable variable |

X's [automation rules](https://help.x.com/en/rules-and-policies/x-automation) condition automatic replies on an initiated/opted-in interaction, an easy opt-out, and one reply per interaction. They prohibit automated likes. They also impose separate approval requirements on AI reply bots and on certain advertiser/publisher/brand auto-response campaigns. This fixed-pool emoji implementation uses no AI reply generation; its checks do not represent approval from X.

## Which interactions qualify

An incoming post must be a **direct reply to a post by the authenticated account that is not itself a reply**, and contain an explicit mention entity for that account's username. The implementation uses account identity and API references, not a plain-text search for a name, to establish the relationship. Replies to the account's own replies are excluded to prevent acknowledgement loops. The requirement is intentionally narrower than all activity in a conversation.

The following do not qualify: a plain mention elsewhere, a quote-only post, a reply to another user's post, an indirect conversation reply, or a direct reply without the explicit own-account mention. This also respects the extra restrictions X currently places on self-serve reply creation. See [X's post-management guide](https://docs.x.com/x-api/posts/manage-tweets/introduction).

The text filters are applied after removing only mentions of the authenticated account. Other `@` mentions and URLs remain excluded. Incoming attachments or sensitive-marked posts are skipped. The configured excluded terms are also checked against the sender's returned display name/username. Question handling, nonempty text, and the default 100-character length limit are checked after the own mention is removed. `filters.txt`, `replies.txt`, and `config.json` contain the actual data and settings; editing them changes which incoming interactions may receive an acknowledgement.

The response is one to three emojis from the configured pool. It does not interpret a question, offer advice, add a promotional link, start a conversation, or mention unrelated users.

## Opt-out and manual replies

Incoming `STOP`, `UNSUBSCRIBE`, and `OPT OUT` requests are handled as opt-outs, not as acknowledgement candidates. Use the command as the message body after the account mention, such as `@YourAccount STOP`; matching ignores case, extra whitespace, and surrounding punctuation. The implementation also recognizes the explicit phrases listed in `xmanage/policy.py`. It does not interpret an arbitrary longer message as an opt-out merely because it contains the word “stop.”

Opt-outs are checked across the fetched incoming batch before acknowledgements, including mention-only opt-out commands. In a live run, an opted-out user's identifier is saved in hashed form and later interactions from that user are suppressed. A dry run shows what would happen without persisting the opt-out.

The manager also inspects your own replies to avoid adding an automated acknowledgement after you have already replied manually. A fresh check reduces collisions, but a human reply published in the short gap after the last check and before the API write is still possible. Keep that limit in mind if you are actively answering the same thread while an automated run is in progress.

## Request, reply, and scan bounds

Defaults:

| Bound | Default | Effect |
| --- | --- | --- |
| Replies per live run | 10 | Maximum new acknowledgements attempted in a pass |
| Reply interval | 15 seconds | Pacing between acknowledgements in a pass |
| X request budget | 60 | Stops a pass from making unbounded API requests |
| Mention pages | 5 | Up to 500 incoming mention items at 100 per page |
| Own-reply pages per check | 10 | Up to 1,000 own timeline items per bounded check; shared request budget still applies |
| Initial lookback | 48 hours | Initial scan horizon before an established cursor exists |
| Scheduled runs | 5 per day | 04:00, 08:00, 12:00, 16:00, 20:00 UTC+7 |

An incomplete paginated scan is not used to authorize replies and does not advance the saved cursor past unseen items. Failures, page limits, and request limits must not silently mark a partial window complete. A large backlog can therefore require operator attention instead of being silently discarded.

Five scheduled runs with a limit of 10 replies permit at most 50 new scheduled acknowledgements per day before manual runs. Most passes should produce fewer because of eligibility, already-answered, opt-out, and filter checks. The request limit likewise permits at most 300 X requests across those five passes, before manual diagnostics or recovery.

These are workload bounds, **not a guaranteed bill ceiling**. A read request may return many billable resources; parent lookups and own-reply checks add work beyond reading incoming mentions. X uses endpoint/resource-dependent credit charging. Monitor credits and spending controls in the [developer console](https://console.x.com); [current pricing](https://docs.x.com/x-api/getting-started/pricing) is the authority. Reusing credentials with the photo uploader means both tools contribute to the same app/account usage.

## Configuration

`config.json` requires these exact fields; unknown or missing fields are rejected. Keep values within the supported ranges and rerun `validate` after editing.

| Field | Default | Allowed range |
| --- | --- | --- |
| `max_replies_per_run` | 10 | 1–50 |
| `reply_interval_seconds` | 15 | 1–300 |
| `max_comment_length` | 100 | 1–500 |
| `emoji_min` | 1 | 1–3 |
| `emoji_max` | 3 | 1–3; at least `emoji_min` |
| `page_size` | 100 | 5–100 |
| `max_mention_pages` | 5 | 1–8 |
| `max_manual_reply_pages` | 10 | 1–32 |
| `request_budget` | 60 | 5–500 |
| `lookback_hours` | 48 | 1–168 |

These are program validation ranges, not recommended increases or platform exemptions. The initial lookback is used when there is no saved cursor; later passes use the cursor. Changing the lookback does not erase existing history. Filter/reply lists accept comma-separated entries or separate lines, with `#` beginning a comment.

## Durable publishing and uncertain outcomes

The live sequence is: validate the interaction, persist an attempt record on `bot-state`, submit the X reply request, then persist its successful outcome. The durable record must succeed before the X create request is sent. The GitHub Contents API is used directly so state does not depend on a later artifact upload or an end-of-job git push.

If a create request times out or its outcome otherwise cannot be established, the manager leaves an unresolved record. Further writes stop until an operator reconciles it. It never assumes that a missing success response means X did not publish the reply. Recovery is documented in [Operations](OPERATIONS.md).

Explicit create rejections such as 400/403 leave a persistent `blocked` record so later runs do not continually resubmit the same rejected content. After fixing the cause, an operator can use the retry resolution. Temporary 402/429 failures are retryable on a later pass; they are not silently discarded. Request-budget exhaustion likewise stops the pass.

Per-interaction record keys and opted-out user identifiers are hashed. The state still needs a raw numeric scan cursor plus your own account and created/manual-reply IDs. It does not contain incoming post text, other users' handles, a media archive, or copied run logs. Hashing reduces direct disclosure; if the repository is public, activity timing and state metadata are public too. State has a 1 MiB limit and stops with a migration error if it grows beyond that limit; preserve it rather than resetting history.

## What verification can establish

- `validate` checks configuration and filter/reply data without X requests or state access. Offline tests exercise the filtering, state transitions, error handling, and mocked API behavior without account credentials or X calls.
- `check` establishes which account the supplied credentials represent and whether it matches the required username.
- `dry-run` exercises actual read access and candidate selection without creating acknowledgements.
- A deliberately observed `live` run is needed to verify the account's actual reply permissions and the full publishing path.

The local photo uploader is a separate tool. A successful test of this manager does not test that uploader's media permissions, and a successful image post does not prove every eligible reply is accepted by X.
