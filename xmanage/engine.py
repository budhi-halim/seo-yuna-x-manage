"""Sequential reply decisions with remote persistence before every public write."""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import logging
import random
import time

from .errors import ApiError, ManagerError, UncertainReply
from .policy import is_opt_out, item_hash, reply_parent_id

UNRESOLVED = {"posting", "uncertain", "blocked"}
TERMINAL = {"ignored", "manual", "replied"}


def now_utc():
    return datetime.now(timezone.utc)


def timestamp(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def validate_account(account, expected, state):
    if not expected or account["username"].casefold() != expected.lstrip("@").casefold():
        raise ManagerError("Authenticated X username does not match X_EXPECTED_USERNAME. No replies were sent.")
    if state["account_id"] not in (None, str(account["id"])):
        raise ManagerError("Stored state belongs to a different X account. Use a separate state/project.")
    state["account_id"] = str(account["id"])


def unresolved(state):
    return {key: value for key, value in state["records"].items() if value["status"] in UNRESOLVED}


def advance_cursor(state, post_id):
    if state["since_id"] is None or int(post_id) > int(state["since_id"]):
        state["since_id"] = str(post_id)


def record(state, key, status, at, *, reason=None, reply_id=None):
    previous = state["records"].get(key, {})
    value = {"status": status, "updated_at": at}
    if previous.get("attempted_at"):
        value["attempted_at"] = previous["attempted_at"]
    if status == "posting":
        value["attempted_at"] = at
    if reason:
        value["reason"] = reason
    if reply_id:
        value["reply_id"] = str(reply_id)
    state["records"][key] = value


def own_reply_map(client, account, oldest_id, settings):
    own = client.timeline("tweets", account["id"], since_id=str(max(1, int(oldest_id) - 1)),
                          page_size=settings["page_size"], max_pages=settings["max_manual_reply_pages"])
    result = {}
    for post in own.posts:
        if str(post.get("author_id", "")) != str(account["id"]):
            # Missing author expansion makes a manual-reply scan unusable.
            raise ManagerError("Own-post scan did not include consistent authorship. No reply will be guessed safe.")
        parent = reply_parent_id(post)
        if parent:
            result[parent] = str(post["id"])
    return result


def run_manager(client, store, settings, policy, reply_tokens, *, expected_username,
                mode="dry-run", live_enabled=False, now_fn=now_utc,
                sleep_fn=time.sleep, rng=None, logger=None):
    if mode not in {"dry-run", "live"}:
        raise ManagerError("Unsupported run mode.")
    live = mode == "live"
    if live and not live_enabled:
        raise ManagerError("Live replies are disabled. Set X_AUTOMATION_ENABLED=true after setup.")
    if not reply_tokens:
        raise ManagerError("replies.txt must contain at least one emoji token.")
    logger = logger or logging.getLogger("xmanage")
    rng = rng or random.SystemRandom()
    account = client.me()
    state = deepcopy(store.load())
    original = deepcopy(state)
    validate_account(account, expected_username, state)
    pending = unresolved(state)
    if pending:
        first = next(iter(pending))
        raise UncertainReply("An earlier reply needs review (record " + first[:16] +
                             "). Use status and the resolution workflow before sending more replies.")
    start = now_fn() - timedelta(hours=settings["lookback_hours"])
    inbox = client.timeline("mentions", account["id"], since_id=state["since_id"],
                            start_time=timestamp(start), page_size=settings["page_size"],
                            max_pages=settings["max_mention_pages"])
    incoming = sorted(inbox.posts, key=lambda post: int(post["id"]))
    stats = {"mode": mode, "seen": len(incoming), "eligible": 0, "replied": 0,
             "manual": 0, "ignored": 0, "already_processed": 0, "new_opt_outs": 0,
             "deferred_by_reply_limit": False, "reasons": {}}
    reasons = Counter()
    # Process the complete fetched batch's opt-outs BEFORE any acknowledgement.
    for post in incoming:
        author_id = str(post.get("author_id", ""))
        if author_id.isdecimal() and is_opt_out(post, account):
            key = item_hash("user", author_id)
            if key not in state["opted_out"]:
                state["opted_out"][key] = {"at": timestamp(now_fn()), "reason": "user_request"}
                stats["new_opt_outs"] += 1
    if live and stats["new_opt_outs"]:
        store.save(state)
    candidates = [post for post in incoming
                  if state["records"].get(item_hash("post", post["id"]), {}).get("status") not in TERMINAL
                  and not is_opt_out(post, account)
                  and policy.evaluate(post, account, inbox.parents, inbox.users, state["opted_out"]) is None]
    manual = own_reply_map(client, account, candidates[0]["id"], settings) if candidates else {}
    for post in incoming:
        post_id = post["id"]
        key = item_hash("post", post_id)
        previous = state["records"].get(key)
        if previous and previous["status"] in TERMINAL:
            stats["already_processed"] += 1
            advance_cursor(state, post_id)
            continue
        reason = "opt_out_command" if is_opt_out(post, account) else policy.evaluate(
            post, account, inbox.parents, inbox.users, state["opted_out"])
        if reason:
            stats["ignored"] += 1
            reasons[reason] += 1
            record(state, key, "ignored", timestamp(now_fn()), reason=reason)
            advance_cursor(state, post_id)
            continue
        if post_id in manual:
            stats["manual"] += 1
            record(state, key, "manual", timestamp(now_fn()), reason="already_replied", reply_id=manual[post_id])
            advance_cursor(state, post_id)
            continue
        stats["eligible"] += 1
        if not live:
            continue
        if stats["replied"] >= settings["max_replies_per_run"]:
            stats["deferred_by_reply_limit"] = True
            break  # Keep the cursor before the first deferred interaction.
        if stats["replied"]:
            sleep_fn(settings["reply_interval_seconds"])
        # Re-fetch the interaction and own replies just before publishing.
        # Manual activity after this check remains an unavoidable remote race.
        try:
            fresh = client.post(post_id)
        except ApiError as error:
            if error.status != 404:
                raise
            stats["ignored"] += 1
            reasons["unavailable"] += 1
            record(state, key, "ignored", timestamp(now_fn()), reason="unavailable")
            advance_cursor(state, post_id)
            continue
        current = fresh.posts[0]
        if str(current.get("author_id", "")) != str(post.get("author_id", "")):
            raise ManagerError("Interaction authorship changed or is missing. No reply was sent.")
        fresh_parents = dict(inbox.parents)
        fresh_parents.update(fresh.parents)
        fresh_users = dict(inbox.users)
        fresh_users.update(fresh.users)
        reason = policy.evaluate(current, account, fresh_parents, fresh_users, state["opted_out"])
        if is_opt_out(current, account):
            state["opted_out"][item_hash("user", str(current["author_id"]))] = {
                "at": timestamp(now_fn()), "reason": "user_request"}
            reason = "opt_out_command"
        if reason:
            stats["ignored"] += 1
            reasons[reason] += 1
            record(state, key, "ignored", timestamp(now_fn()), reason=reason)
            advance_cursor(state, post_id)
            continue
        manual.update(own_reply_map(client, account, post_id, settings))
        if post_id in manual:
            stats["manual"] += 1
            record(state, key, "manual", timestamp(now_fn()), reason="already_replied", reply_id=manual[post_id])
            advance_cursor(state, post_id)
            continue
        text = "".join(rng.choices(reply_tokens, k=rng.randint(settings["emoji_min"], settings["emoji_max"])))
        record(state, key, "posting", timestamp(now_fn()))
        logger.info("Preparing acknowledgement for source Post ID %s (record %s).", post_id, key[:16])
        # This write must complete remotely before the POST can happen.
        store.save(state)
        try:
            reply_id = client.reply(text, post_id)
            if not str(reply_id).isdecimal():
                raise ApiError("Reply result had an invalid ID.", ambiguous=True)
        except ApiError as error:
            if error.ambiguous:
                record(state, key, "uncertain", timestamp(now_fn()), reason="unknown_outcome")
                store.save(state)
                raise UncertainReply("Reply outcome uncertain; no automatic retry. Record " + key[:16] +
                                     ". Inspect X and use the resolution workflow.") from error
            status = "retry" if error.status in {None, 402, 429} else "blocked"
            record(state, key, status, timestamp(now_fn()), reason="api_rejected")
            store.save(state)
            raise
        except (Exception, KeyboardInterrupt) as error:
            record(state, key, "uncertain", timestamp(now_fn()), reason="interrupted_create")
            store.save(state)
            if isinstance(error, KeyboardInterrupt):
                raise
            raise UncertainReply("Reply request interrupted; inspect X before resolving record " + key[:16]) from error
        record(state, key, "replied", timestamp(now_fn()), reply_id=reply_id)
        advance_cursor(state, post_id)
        # If this fails, the previous remote 'posting' record blocks a duplicate.
        store.save(state)
        stats["replied"] += 1
        logger.info("Published acknowledgement %d in this run.", stats["replied"])
    if live and state != original:
        store.save(state)
    stats["reasons"] = dict(sorted(reasons.items()))
    stats["requests_used"] = getattr(client, "requests_used", 0)
    return stats


def resolve_reply(client, store, *, expected_username, source_post_id, reply_post_id=None,
                  retry=False, now_fn=now_utc):
    if bool(reply_post_id) == bool(retry):
        raise ManagerError("Choose either the existing reply ID or retry after checking X.")
    account = client.me()
    state = store.load()
    validate_account(account, expected_username, state)
    key = item_hash("post", source_post_id)
    existing = state["records"].get(key)
    if not existing or existing["status"] not in UNRESOLVED:
        raise ManagerError("That source Post ID does not identify an unresolved record.")
    if reply_post_id:
        post = client.post(reply_post_id).posts[0]
        if str(post.get("author_id", "")) != str(account["id"]) or reply_parent_id(post) != str(source_post_id):
            raise ManagerError("The supplied reply must be this account's direct reply to that exact interaction.")
        record(state, key, "replied", timestamp(now_fn()), reply_id=reply_post_id)
        # Do not jump over intervening interactions by advancing the global cursor.
    else:
        record(state, key, "retry", timestamp(now_fn()), reason="operator_retry")
    store.save(state)
    return {"record": key[:16], "status": state["records"][key]["status"]}
