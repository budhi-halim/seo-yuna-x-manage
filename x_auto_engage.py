#!/usr/bin/env python3
"""Manage scoped X replies. Default run mode is read-only dry-run."""
import argparse
from collections import Counter
import json
import logging
import os
from pathlib import Path
import re
import sys
import unicodedata

from xmanage.alerts import maybe_send_alert
from xmanage.client import XClient
from xmanage.config import credentials_from_env, expected_username, load_config, load_list
from xmanage.engine import resolve_reply, run_manager
from xmanage.errors import ApiError, ManagerError
from xmanage.policy import ContentPolicy
from xmanage.state import GitHubStateStore, LocalStateStore, StateError

BASE_DIR = Path(__file__).resolve().parent


def make_store(env):
    backend = env.get("STATE_BACKEND", "github" if env.get("GITHUB_ACTIONS") == "true" else "local")
    if backend == "local":
        if env.get("GITHUB_ACTIONS") == "true":
            raise ManagerError("GitHub Actions must use persistent GitHub state, not an ephemeral local file.")
        return LocalStateStore(BASE_DIR / "state" / "local_state.json")
    if backend != "github":
        raise ManagerError("STATE_BACKEND must be local or github.")
    repo = env.get("GITHUB_REPOSITORY", "")
    token = env.get("GITHUB_TOKEN", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not token:
        raise ManagerError("GitHub state requires GITHUB_REPOSITORY and the workflow's built-in GITHUB_TOKEN.")
    return GitHubStateStore(repo, token, base_sha=env.get("GITHUB_SHA") or None)


def setup_content():
    settings = load_config(BASE_DIR)
    words = load_list(BASE_DIR / "filters.txt")
    replies = load_list(BASE_DIR / "replies.txt")
    # This bot selects static emojis; it never generates AI text or sends links.
    if not replies or any(not 1 <= len(token) <= 16 or not all(
            unicodedata.category(character) in {"So", "Sk", "Mn", "Me", "Cf"}
            for character in token) for token in replies):
        raise ManagerError("replies.txt must contain comma/newline-separated emoji tokens (up to 16 code points each).")
    return settings, ContentPolicy(words, settings["max_comment_length"]), replies


def parser():
    result = argparse.ArgumentParser(description="Scoped X emoji replies using the official API and durable history.")
    sub = result.add_subparsers(dest="command")
    sub.add_parser("validate", help="Validate local configuration and reply/filter files; no API calls")
    sub.add_parser("check", help="Check authenticated account; no reply or state changes")
    run = sub.add_parser("run", help="Scan interactions; dry-run by default")
    run.add_argument("--mode", choices=["dry-run", "live"], default="dry-run")
    sub.add_parser("status", help="Read saved state; no X API calls")
    resolve = sub.add_parser("resolve", help="Resolve a blocked/uncertain reply after inspecting X")
    resolve.add_argument("--source-post-id", required=True)
    group = resolve.add_mutually_exclusive_group(required=True)
    group.add_argument("--reply-post-id")
    group.add_argument("--retry", action="store_true")
    return result


def report(value):
    text = json.dumps(value, ensure_ascii=False, indent=2)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write("## X reply manager\n\n```json\n" + text + "\n```\n")
        except OSError:
            logging.warning("Could not append the GitHub step summary.")


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s", stream=sys.stdout)
    arguments = parser()
    args = arguments.parse_args(argv)
    if not args.command:
        arguments.print_help()
        return 0
    client = None
    try:
        if args.command == "status":
            state = make_store(os.environ).load()
            counts = Counter(record["status"] for record in state["records"].values())
            pending = [{"record": key[:16], "status": record["status"], "updated_at": record["updated_at"]}
                       for key, record in state["records"].items()
                       if record["status"] in {"posting", "uncertain", "blocked"}]
            report({"initialized": state["account_id"] is not None, "records": dict(sorted(counts.items())),
                    "opted_out_users": len(state["opted_out"]), "needs_review": pending})
            return 0
        settings, policy, replies = setup_content()
        if args.command == "validate":
            report({"configuration": "valid", "reply_tokens": len(replies), "network_requests": 0})
            return 0
        live_enabled = os.environ.get("X_AUTOMATION_ENABLED", "").strip().lower() == "true"
        if args.command == "run" and args.mode == "live" and not live_enabled:
            raise ManagerError("Live replies are disabled. Set repository variable X_AUTOMATION_ENABLED=true after setup.")
        expected = expected_username(os.environ)
        client = XClient(credentials_from_env(os.environ), settings["request_budget"])
        if args.command == "check":
            account = client.me()
            if account["username"].casefold() != expected.casefold():
                raise ManagerError("Authenticated account does not match X_EXPECTED_USERNAME.")
            report({"authenticated_as": "@" + account["username"], "write_permission_tested": False,
                    "state_changed": False, "replies_sent": 0})
            return 0
        store = make_store(os.environ)
        if args.command == "run":
            report(run_manager(client, store, settings, policy, replies, expected_username=expected,
                               mode=args.mode, live_enabled=live_enabled))
        else:
            for identifier in (args.source_post_id, args.reply_post_id):
                if identifier is not None and not re.fullmatch(r"[0-9]{1,20}", identifier):
                    raise ManagerError("Use numeric Post IDs from the actual X URLs.")
            report(resolve_reply(client, store, expected_username=expected,
                                 source_post_id=args.source_post_id, reply_post_id=args.reply_post_id,
                                 retry=args.retry))
        return 0
    except (ManagerError, StateError, OSError) as error:
        logging.error("%s", error)
        # Read-only/manual diagnostic modes never send email or modify cooldown state.
        if (args.command == "run" and args.mode == "live" and isinstance(error, ApiError)
                and error.status in {401, 402, 403}):
            try:
                outcome = maybe_send_alert(os.environ, make_store(os.environ), status=error.status)
                logging.info("Optional email alert: %s", outcome)
            except Exception as alert_error:
                logging.warning("Optional email alert failed (%s). Check this Actions run.", type(alert_error).__name__)
        return 1
    except KeyboardInterrupt:
        logging.warning("Stopped. Review status before resuming if a reply request was in flight.")
        return 130
    except Exception as error:
        # Do not leak credential-bearing request objects through a traceback.
        logging.error("Unexpected %s. Check saved status before retrying a live run.", type(error).__name__)
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
