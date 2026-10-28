"""Public-reply safety invariants with an in-memory durable store and fake X."""
from copy import deepcopy
from datetime import datetime, timezone
import random
import unittest

from xmanage.client import Timeline
from xmanage.config import DEFAULTS
from xmanage.engine import resolve_reply, run_manager
from xmanage.errors import ApiError, ManagerError, UncertainReply
from xmanage.policy import ContentPolicy, item_hash
from xmanage.state import StateError, initial_state, validate_state


ACCOUNT = {"id": "10", "username": "SeoYuna"}
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
PARENT = {"id": "100", "author_id": "10", "text": "An original post"}
READER = {"id": "20", "username": "reader", "name": "Reader"}


def interaction(identifier="200", author="20", text="@SeoYuna Beautiful photo!"):
    return {"id": identifier, "author_id": author, "text": text,
            "in_reply_to_user_id": "10", "referenced_posts": [{"type": "replied_to", "id": "100"}],
            "entities": {"mentions": [{"id": "10", "username": "SeoYuna"}]},
            "possibly_sensitive": False}


def owner_reply(source="200", identifier="800", *, legacy=False):
    return {"id": identifier, "author_id": "10", "text": "Thanks!",
            "referenced_tweets" if legacy else "referenced_posts": [{"type": "replied_to", "id": source}]}


def inbox(posts):
    return Timeline(deepcopy(posts), {"100": deepcopy(PARENT)}, {"20": deepcopy(READER)})


class MemoryStore:
    """A save either updates an independent durable snapshot or raises before it."""
    def __init__(self, state=None, *, fail_saves=(), events=None):
        self.remote = deepcopy(initial_state() if state is None else state)
        self.fail_saves = set(fail_saves)
        self.events = events if events is not None else []
        self.snapshots = []
        self.save_attempts = 0

    def load(self):
        self.events.append(("load",))
        return deepcopy(self.remote)

    def save(self, state):
        validate_state(state)
        self.save_attempts += 1
        self.events.append(("save", self.save_attempts))
        if self.save_attempts in self.fail_saves:
            raise StateError("Injected unconfirmed state write.")
        self.remote = deepcopy(state)
        self.snapshots.append(deepcopy(state))


class FakeClient:
    """Every operation is local; response queues let tests introduce races."""
    def __init__(self, incoming=None, *, own_scans=(), fresh=None, outcomes=(), events=None):
        self.incoming = deepcopy(inbox([]) if incoming is None else incoming)
        self.own_scans = list(own_scans)
        self.fresh = deepcopy(fresh or {})
        self.outcomes = list(outcomes)
        self.events = events if events is not None else []
        self.account = deepcopy(ACCOUNT)
        self.calls = []
        self.public_writes = []
        self.requests_used = 0
        self.before_reply = None

    def _called(self, name, *args):
        self.calls.append((name, *args))
        self.events.append((name, *args))
        self.requests_used += 1

    def me(self):
        self._called("me")
        return deepcopy(self.account)

    def timeline(self, kind, user_id, **kwargs):
        self._called("timeline", kind, user_id, kwargs)
        if kind == "mentions":
            return deepcopy(self.incoming)
        if kind != "tweets":
            raise AssertionError("Unexpected endpoint")
        response = self.own_scans.pop(0) if self.own_scans else Timeline()
        if isinstance(response, BaseException):
            raise response
        return deepcopy(response)

    def post(self, identifier):
        self._called("post", identifier)
        response = self.fresh.get(identifier)
        if response is None:
            found = [post for post in self.incoming.posts if post["id"] == identifier]
            if not found:
                raise AssertionError("Unexpected single-post lookup")
            response = Timeline(found, self.incoming.parents, self.incoming.users)
        if isinstance(response, BaseException):
            raise response
        return deepcopy(response)

    def reply(self, text, source_post_id):
        self._called("reply", source_post_id)
        if self.before_reply:
            self.before_reply(source_post_id)
        self.public_writes.append((source_post_id, text))
        response = self.outcomes.pop(0) if self.outcomes else str(900 + len(self.public_writes))
        if isinstance(response, BaseException):
            raise response
        return response


class EngineTests(unittest.TestCase):
    def run_bot(self, client, store, *, mode="live", enabled=True, overrides=None, expected="SeoYuna"):
        settings = dict(DEFAULTS)
        settings.update(overrides or {})
        return run_manager(client, store, settings, ContentPolicy(["spam"]), ["😊", "💛"],
                           expected_username=expected, mode=mode, live_enabled=enabled,
                           now_fn=lambda: NOW, sleep_fn=lambda _: None, rng=random.Random(1))

    def test_live_flag_and_account_identity_are_required_before_public_actions(self):
        client, store = FakeClient(inbox([interaction()])), MemoryStore()
        with self.assertRaisesRegex(ManagerError, "disabled"):
            self.run_bot(client, store, enabled=False)
        self.assertEqual(client.calls, [])
        with self.assertRaisesRegex(ManagerError, "does not match"):
            self.run_bot(client, store, expected="AnotherAccount")
        self.assertEqual(client.public_writes, [])
        self.assertEqual(store.save_attempts, 0)
        self.assertFalse(any(call[0] == "timeline" for call in client.calls))

    def test_dry_run_never_saves_state_or_publishes(self):
        stop = interaction("201", author="30", text="@SeoYuna STOP")
        client = FakeClient(inbox([interaction(), stop]))
        store = MemoryStore()
        before = deepcopy(store.remote)
        result = self.run_bot(client, store, mode="dry-run")
        self.assertEqual(result["eligible"], 1)
        self.assertEqual(result["new_opt_outs"], 1)
        self.assertEqual(result["replied"], 0)
        self.assertEqual(client.public_writes, [])
        self.assertEqual(store.save_attempts, 0)
        self.assertEqual(store.remote, before)

    def test_intent_is_durable_before_public_post(self):
        events = []
        client, store = FakeClient(inbox([interaction()]), events=events), MemoryStore(events=events)
        def assert_intent(source):
            record = store.remote["records"][item_hash("post", source)]
            self.assertEqual(record["status"], "posting")
            self.assertIn("attempted_at", record)
            self.assertIsNone(store.remote["since_id"])
        client.before_reply = assert_intent
        result = self.run_bot(client, store)
        self.assertEqual(result["replied"], 1)
        self.assertLess(events.index(("save", 1)), events.index(("reply", "200")))
        self.assertEqual(store.snapshots[0]["records"][item_hash("post", "200")]["status"], "posting")
        self.assertEqual(store.remote["records"][item_hash("post", "200")]["status"], "replied")
        self.assertEqual(store.remote["since_id"], "200")

    def test_unconfirmed_intent_write_prevents_public_post(self):
        client, store = FakeClient(inbox([interaction()])), MemoryStore(fail_saves={1})
        with self.assertRaises(StateError):
            self.run_bot(client, store)
        self.assertEqual(client.public_writes, [])
        self.assertEqual(store.remote, initial_state())

    def test_ambiguous_outcome_blocks_future_runs_even_if_source_disappears(self):
        client = FakeClient(inbox([interaction()]), outcomes=[ApiError("Timeout", ambiguous=True)])
        store = MemoryStore()
        with self.assertRaises(UncertainReply):
            self.run_bot(client, store)
        self.assertEqual(len(client.public_writes), 1)
        self.assertEqual(store.remote["records"][item_hash("post", "200")]["status"], "uncertain")
        self.assertIsNone(store.remote["since_id"])
        later = FakeClient(inbox([]))
        with self.assertRaises(UncertainReply):
            self.run_bot(later, store)
        self.assertEqual(later.public_writes, [])
        self.assertFalse(any(call[0] == "timeline" for call in later.calls))

    def test_all_unresolved_states_block_before_inbox_discovery(self):
        for status in ("posting", "uncertain", "blocked"):
            with self.subTest(status=status):
                state = initial_state()
                state["account_id"] = "10"
                state["records"][item_hash("post", "200")] = {
                    "status": status, "updated_at": NOW.isoformat()}
                client, store = FakeClient(inbox([])), MemoryStore(state)
                with self.assertRaises(UncertainReply):
                    self.run_bot(client, store)
                self.assertEqual([call[0] for call in client.calls], ["me"])
                self.assertEqual(store.save_attempts, 0)

    def test_existing_manual_reply_is_skipped_including_legacy_reference(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                client = FakeClient(inbox([interaction()]), own_scans=[Timeline([owner_reply(legacy=legacy)])])
                store = MemoryStore()
                result = self.run_bot(client, store)
                self.assertEqual(result["manual"], 1)
                self.assertEqual(client.public_writes, [])
                self.assertEqual(store.remote["records"][item_hash("post", "200")]["status"], "manual")
                self.assertFalse(any(call[0] == "post" for call in client.calls))

    def test_manual_reply_created_during_run_is_detected_before_post(self):
        client = FakeClient(inbox([interaction()]), own_scans=[Timeline(), Timeline([owner_reply()])])
        store = MemoryStore()
        result = self.run_bot(client, store)
        self.assertEqual(result["manual"], 1)
        self.assertEqual(client.public_writes, [])
        self.assertEqual(store.remote["records"][item_hash("post", "200")]["reply_id"], "800")

    def test_newer_stop_prevents_reply_to_older_praise_in_same_batch(self):
        stop = interaction("201", text="@SeoYuna STOP")
        stop["referenced_posts"] = []  # Opt-out is not limited to eligible direct replies.
        client, store = FakeClient(inbox([stop, interaction()])), MemoryStore()
        result = self.run_bot(client, store)
        self.assertEqual(result["new_opt_outs"], 1)
        self.assertEqual(client.public_writes, [])
        self.assertIn(item_hash("user", "20"), store.remote["opted_out"])
        self.assertEqual(store.remote["records"][item_hash("post", "200")]["reason"], "opted_out")
        self.assertEqual(store.remote["records"][item_hash("post", "201")]["reason"], "opt_out_command")

    def test_reply_limit_preserves_cursor_before_first_deferred_post(self):
        client, store = FakeClient(inbox([interaction("201"), interaction("200")])), MemoryStore()
        result = self.run_bot(client, store, overrides={"max_replies_per_run": 1})
        self.assertTrue(result["deferred_by_reply_limit"])
        self.assertEqual([source for source, _ in client.public_writes], ["200"])
        self.assertEqual(store.remote["since_id"], "200")
        self.assertNotIn(item_hash("post", "201"), store.remote["records"])
        later = FakeClient(inbox([interaction("201")]))
        self.assertEqual(self.run_bot(later, store)["replied"], 1)
        self.assertEqual(store.remote["since_id"], "201")

    def test_rejected_403_is_blocked_and_not_repeated(self):
        client = FakeClient(inbox([interaction()]), outcomes=[ApiError("Forbidden", status=403)])
        store = MemoryStore()
        with self.assertRaises(ApiError):
            self.run_bot(client, store)
        self.assertEqual(store.remote["records"][item_hash("post", "200")]["status"], "blocked")
        later = FakeClient(inbox([interaction()]))
        with self.assertRaises(UncertainReply):
            self.run_bot(later, store)
        self.assertEqual(later.public_writes, [])

    def test_explicit_429_is_retryable_without_false_uncertainty(self):
        client = FakeClient(inbox([interaction()]), outcomes=[ApiError("Rate limited", status=429)])
        store = MemoryStore()
        with self.assertRaises(ApiError):
            self.run_bot(client, store)
        self.assertEqual(store.remote["records"][item_hash("post", "200")]["status"], "retry")
        self.assertIsNone(store.remote["since_id"])
        later = FakeClient(inbox([interaction()]))
        self.assertEqual(self.run_bot(later, store)["replied"], 1)
        self.assertEqual(len(later.public_writes), 1)

    def test_success_with_failed_completion_save_keeps_durable_posting_guard(self):
        client, store = FakeClient(inbox([interaction()])), MemoryStore(fail_saves={2})
        with self.assertRaises(StateError):
            self.run_bot(client, store)
        self.assertEqual(len(client.public_writes), 1)
        self.assertEqual(store.remote["records"][item_hash("post", "200")]["status"], "posting")
        self.assertIsNone(store.remote["since_id"])
        later = FakeClient(inbox([]))
        with self.assertRaises(UncertainReply):
            self.run_bot(later, store)
        self.assertEqual(later.public_writes, [])

    def test_unreliable_manual_scan_cannot_authorize_a_reply(self):
        broken_own = Timeline([{"id": "800", "referenced_posts": [{"type": "replied_to", "id": "200"}]}])
        client, store = FakeClient(inbox([interaction()]), own_scans=[broken_own]), MemoryStore()
        with self.assertRaisesRegex(ManagerError, "authorship"):
            self.run_bot(client, store)
        self.assertEqual(client.public_writes, [])
        self.assertEqual(store.save_attempts, 0)

    def test_final_recheck_skips_deleted_or_now_ineligible_source(self):
        changed = interaction(text="@SeoYuna How are you?")
        for response, reason in ((ApiError("Gone", status=404), "unavailable"),
                                 (inbox([changed]), "question")):
            with self.subTest(reason=reason):
                client = FakeClient(inbox([interaction()]), fresh={"200": response})
                store = MemoryStore()
                result = self.run_bot(client, store)
                self.assertEqual(result["ignored"], 1)
                self.assertEqual(client.public_writes, [])
                self.assertEqual(store.remote["records"][item_hash("post", "200")]["reason"], reason)
                self.assertEqual(store.remote["since_id"], "200")

    def test_resolution_requires_own_direct_reply_and_does_not_jump_cursor(self):
        state = initial_state()
        state["account_id"], state["since_id"] = "10", "150"
        state["records"][item_hash("post", "200")] = {"status": "uncertain", "updated_at": NOW.isoformat()}
        wrong = owner_reply(source="199")
        client = FakeClient(fresh={"800": Timeline([wrong])})
        store = MemoryStore(state)
        with self.assertRaises(ManagerError):
            resolve_reply(client, store, expected_username="SeoYuna", source_post_id="200", reply_post_id="800")
        self.assertEqual(store.save_attempts, 0)
        client = FakeClient(fresh={"800": Timeline([owner_reply()])})
        result = resolve_reply(client, store, expected_username="SeoYuna", source_post_id="200",
                               reply_post_id="800", now_fn=lambda: NOW)
        self.assertEqual(result["status"], "replied")
        self.assertEqual(store.remote["since_id"], "150")
        self.assertEqual(client.public_writes, [])


if __name__ == "__main__":
    unittest.main()
