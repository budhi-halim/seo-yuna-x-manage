"""Offline persistence tests: no GitHub or X network requests are performed."""

from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import requests


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from xmanage import state as module
from xmanage.state import GitHubStateStore, LocalStateStore, StateError, initial_state


NOW = "2026-10-28T12:34:56+00:00"
POST_HASH = hashlib.sha256(b"post:1111111111111111111").hexdigest()
USER_HASH = hashlib.sha256(b"user:2222222222222222222").hexdigest()
BASE_SHA = "a" * 40
OLD_SHA = "b" * 40
NEW_SHA = "c" * 40
NEXT_SHA = "d" * 40
TOKEN = "ghs_only_a_dummy_github_token_for_offline_tests"


def populated_state():
    state = initial_state()
    state.update({
        "account_id": "3333333333333333333",
        "since_id": "1111111111111111111",
        "records": {
            POST_HASH: {
                "status": "posting", "updated_at": NOW, "attempted_at": NOW,
                "reply_id": None, "reason": None, "author_hash": USER_HASH,
            }
        },
        "opted_out": {USER_HASH: {"at": NOW, "reason": "user_request"}},
        "last_alert_at": NOW,
    })
    return state


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self.payload = payload

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return deepcopy(self.payload)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {"Authorization": "Bearer not-the-github-token"}
        self.auth = ("old-auth", "must-not-be-used")
        self.trust_env = True
        self.cookies = {"session": "old-cookie"}
        self.closed = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, deepcopy(kwargs)))
        if not self.responses:
            raise AssertionError("Unexpected extra HTTP request; writes must never auto-retry")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


def branch_response(status=200, *, branch="bot-state", sha=BASE_SHA):
    return FakeResponse(status, {"ref": f"refs/heads/{branch}", "object": {"sha": sha}})


def content_response(state=None, *, sha=OLD_SHA, raw=None):
    if raw is None:
        raw = json.dumps(populated_state() if state is None else state).encode("utf-8")
    return FakeResponse(200, {
        "type": "file", "encoding": "base64", "sha": sha,
        "content": base64.encodebytes(raw).decode("ascii"), "size": len(raw),
    })


def saved_response(status=200, *, sha=NEW_SHA):
    return FakeResponse(status, {"content": {"sha": sha}, "commit": {"sha": NEXT_SHA}})


class LocalStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state" / "engagement.json"
        self.store = LocalStateStore(self.path)

    def test_missing_local_file_returns_independent_initial_state(self):
        first = self.store.load()
        self.assertEqual(first, {
            "version": 1, "account_id": None, "since_id": None,
            "records": {}, "opted_out": {}, "last_alert_at": None,
        })
        first["records"][POST_HASH] = {"status": "manual", "updated_at": NOW}
        self.assertEqual(self.store.load()["records"], {})
        self.assertFalse(self.path.exists())

    def test_saved_bytes_are_complete_readable_and_round_trip_exactly(self):
        state = populated_state()
        self.store.save(state)
        raw = self.path.read_bytes()
        self.assertEqual(json.loads(raw), state)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(LocalStateStore(self.path).load(), state)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        self.assertNotIn(TOKEN.encode(), raw)

    def test_corrupt_existing_file_is_not_silently_reset_or_changed(self):
        self.path.parent.mkdir()
        for raw in (b"not JSON", b"[]", b'{"version": 1}', b'\xff\xfeinvalid'):
            with self.subTest(raw=repr(raw)):
                self.path.write_bytes(raw)
                with self.assertRaises(StateError):
                    self.store.load()
                self.assertEqual(self.path.read_bytes(), raw)

    def test_duplicate_json_keys_and_non_json_numbers_are_rejected(self):
        self.path.parent.mkdir()
        valid = json.dumps(initial_state())
        candidates = (
            valid.replace('"version": 1', '"version": 1, "version": 1'),
            valid.replace('"account_id": null', '"account_id": NaN'),
        )
        for text in candidates:
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                with self.assertRaises(StateError):
                    self.store.load()

    def test_invalid_fields_never_replace_previously_valid_state(self):
        valid = populated_state()
        self.store.save(valid)
        original = self.path.read_bytes()
        cases = []

        def changed(mutator):
            state = deepcopy(valid)
            mutator(state)
            cases.append(state)

        changed(lambda s: s.pop("since_id"))
        changed(lambda s: s.update({"auth": "must_never_be_saved"}))
        changed(lambda s: s.update({"version": True}))
        changed(lambda s: s.update({"version": 2}))
        changed(lambda s: s.update({"account_id": 123}))
        changed(lambda s: s.update({"since_id": "@someone"}))
        changed(lambda s: s.update({"records": []}))
        changed(lambda s: s["records"].update({"raw-post-id": {"status": "manual", "updated_at": NOW}}))
        changed(lambda s: s["records"][POST_HASH].update({"text": "Do not store this reply text"}))
        changed(lambda s: s["records"][POST_HASH].pop("updated_at"))
        changed(lambda s: s["records"][POST_HASH].update({"status": "unrecognized"}))
        changed(lambda s: s["records"][POST_HASH].update({"updated_at": "2026-10-28T12:34:56"}))
        changed(lambda s: s["records"][POST_HASH].update({"attempted_at": "2026-10-28T12:34:56+07:00"}))
        changed(lambda s: s["records"][POST_HASH].update({"reply_id": 123}))
        changed(lambda s: s["records"][POST_HASH].update({"reason": "HTTP 403: raw private detail"}))
        changed(lambda s: s["records"][POST_HASH].update({"author_hash": "@someone"}))
        changed(lambda s: s["opted_out"].update({"raw-user-id": {"at": NOW, "reason": "configured"}}))
        changed(lambda s: s["opted_out"][USER_HASH].update({"reason": "raw message"}))
        changed(lambda s: s.update({"last_alert_at": "2026-02-30T12:34:56Z"}))
        for number, state in enumerate(cases):
            with self.subTest(case=number):
                with self.assertRaises(StateError):
                    self.store.save(state)
                self.assertEqual(self.path.read_bytes(), original)

    def test_blocked_record_and_nullable_optional_fields_are_valid(self):
        state = populated_state()
        state["records"][POST_HASH] = {
            "status": "blocked", "updated_at": "2026-10-28T12:34:56Z",
            "attempted_at": None, "reply_id": None, "reason": "api_rejected",
        }
        self.store.save(state)
        self.assertEqual(self.store.load(), state)

    def test_failed_atomic_replace_preserves_previous_file(self):
        self.store.save(initial_state())
        original = self.path.read_bytes()
        with mock.patch.object(module.os, "replace", side_effect=OSError("Simulated disk failure")):
            with self.assertRaises(StateError):
                self.store.save(populated_state())
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], [self.path.name])

    def test_oversized_state_is_rejected_before_local_write(self):
        self.store.save(initial_state())
        original = self.path.read_bytes()
        with mock.patch.object(module, "MAX_STATE_BYTES", 100):
            with self.assertRaises(StateError):
                self.store.save(populated_state())
        self.assertEqual(self.path.read_bytes(), original)


class GitHubStateTests(unittest.TestCase):
    def store(self, responses, *, base_sha=BASE_SHA):
        session = FakeSession(responses)
        store = GitHubStateStore("owner/repository", TOKEN, base_sha=base_sha, session=session)
        return store, session

    def test_existing_file_loads_using_fixed_host_explicit_ref_and_github_auth_only(self):
        store, session = self.store([branch_response(), content_response()])
        self.assertEqual(store.load(), populated_state())
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[1][0:2], (
            "GET", "https://api.github.com/repos/owner/repository/contents/state/engagement.json",
        ))
        self.assertEqual(session.calls[1][2]["params"], {"ref": "bot-state"})
        for method, url, options in session.calls:
            self.assertTrue(url.startswith("https://api.github.com/"))
            self.assertEqual(options["headers"]["Authorization"], f"Bearer {TOKEN}")
            self.assertFalse(options["allow_redirects"])
            self.assertEqual(options["timeout"], (10, 30))
        self.assertIsNone(session.auth)
        self.assertFalse(session.trust_env)
        self.assertEqual(session.cookies, {})
        self.assertEqual(session.headers["Authorization"], f"Bearer {TOKEN}")

    def test_each_successful_save_uses_and_advances_compare_and_swap_sha(self):
        store, session = self.store([
            branch_response(), content_response(), saved_response(), saved_response(sha=NEXT_SHA),
        ])
        state = store.load()
        store.save(state)
        state["records"][POST_HASH].update({"status": "replied", "reply_id": "4444444444444444444"})
        store.save(state)
        first_body = session.calls[2][2]["json"]
        second_body = session.calls[3][2]["json"]
        self.assertEqual(first_body["sha"], OLD_SHA)
        self.assertEqual(second_body["sha"], NEW_SHA)
        self.assertEqual(second_body["branch"], "bot-state")
        self.assertEqual(json.loads(base64.b64decode(second_body["content"])), state)
        self.assertEqual(json.loads(base64.b64decode(first_body["content"]))["records"][POST_HASH]["status"], "posting")
        self.assertEqual([call[0] for call in session.calls], ["GET", "GET", "PUT", "PUT"])

    def test_missing_file_on_existing_branch_never_becomes_empty_state(self):
        store, session = self.store([branch_response(), FakeResponse(404, {"message": "Not Found"})])
        with self.assertRaises(StateError):
            store.load()
        with self.assertRaises(StateError):
            store.save(initial_state())
        self.assertEqual(len(session.calls), 2)

    def test_new_branch_bootstrap_allows_one_new_file_without_a_sha(self):
        store, session = self.store([
            FakeResponse(404, {}), branch_response(201), saved_response(201),
        ])
        self.assertEqual(store.load(), initial_state())
        self.assertEqual([call[0] for call in session.calls], ["GET"])
        store.save(populated_state())
        self.assertEqual([call[0] for call in session.calls], ["GET", "POST", "PUT"])
        self.assertEqual(session.calls[1][1], "https://api.github.com/repos/owner/repository/git/refs")
        self.assertEqual(session.calls[1][2]["json"], {"ref": "refs/heads/bot-state", "sha": BASE_SHA})
        self.assertNotIn("sha", session.calls[2][2]["json"])
        self.assertEqual(json.loads(base64.b64decode(session.calls[2][2]["json"]["content"])), populated_state())

    def test_bootstrap_uses_github_sha_when_no_explicit_base_is_supplied(self):
        store, session = self.store([
            FakeResponse(404, {}), branch_response(201, sha=NEXT_SHA), saved_response(201),
        ], base_sha=None)
        with mock.patch.dict(os.environ, {"GITHUB_SHA": NEXT_SHA}):
            self.assertEqual(store.load(), initial_state())
        self.assertEqual([call[0] for call in session.calls], ["GET"])
        store.save(initial_state())
        self.assertEqual(session.calls[1][2]["json"]["sha"], NEXT_SHA)
        self.assertEqual(len(session.calls), 3)

    def test_bootstrap_falls_back_to_repository_default_branch(self):
        store, session = self.store([
            FakeResponse(404, {}), FakeResponse(200, {"default_branch": "main"}),
            branch_response(branch="main"), branch_response(201), saved_response(201),
        ], base_sha=None)
        with mock.patch.dict(os.environ, {"GITHUB_SHA": ""}):
            self.assertEqual(store.load(), initial_state())
        self.assertEqual([call[0] for call in session.calls], ["GET", "GET", "GET"])
        store.save(initial_state())
        self.assertEqual(session.calls[1][1], "https://api.github.com/repos/owner/repository")
        self.assertTrue(session.calls[2][1].endswith("/git/ref/heads/main"))
        self.assertEqual(session.calls[3][2]["json"]["sha"], BASE_SHA)

    def test_branch_creation_conflict_is_not_automatically_retried_or_overwritten(self):
        store, session = self.store([FakeResponse(404, {}), FakeResponse(422, {"message": "Reference already exists"})])
        state = store.load()
        with self.assertRaises(StateError):
            store.save(state)
        with self.assertRaises(StateError):
            store.load()
        self.assertEqual([call[0] for call in session.calls], ["GET", "POST"])

    def test_branch_creation_timeout_stops_before_loading_or_saving_empty_state(self):
        store, session = self.store([FakeResponse(404, {}), requests.Timeout("Lost branch creation response")])
        state = store.load()
        with self.assertRaises(StateError):
            store.save(state)
        with self.assertRaises(StateError):
            store.save(initial_state())
        self.assertEqual(len(session.calls), 2)

    def test_first_file_creation_cannot_overwrite_a_concurrent_file(self):
        store, session = self.store([
            FakeResponse(404, {}), branch_response(201),
            FakeResponse(422, {"message": "sha was not supplied"}),
        ])
        state = store.load()
        with self.assertRaises(StateError):
            store.save(state)
        with self.assertRaises(StateError):
            store.save(state)
        self.assertEqual(len(session.calls), 3)
        self.assertNotIn("sha", session.calls[2][2]["json"])

    def test_creation_then_file_save_failure_requires_existing_branch_recovery(self):
        store, session = self.store([
            FakeResponse(404, {}), branch_response(201), requests.Timeout("Lost first file response"),
        ])
        state = store.load()
        with self.assertRaises(StateError):
            store.save(state)
        self.assertEqual([call[0] for call in session.calls], ["GET", "POST", "PUT"])
        fresh, fresh_session = self.store([branch_response(), FakeResponse(404, {})])
        with self.assertRaises(StateError):
            fresh.load()
        self.assertEqual([call[0] for call in fresh_session.calls], ["GET", "GET"])

    def test_stale_sha_conflict_stops_without_refreshing_or_retrying(self):
        for status in (409, 422):
            with self.subTest(status=status):
                store, session = self.store([
                    branch_response(), content_response(),
                    FakeResponse(status, {"message": "SHA conflict " + TOKEN}),
                ])
                state = store.load()
                with self.assertRaises(StateError) as raised:
                    store.save(state)
                self.assertNotIn(TOKEN, str(raised.exception))
                with self.assertRaises(StateError):
                    store.save(state)
                self.assertEqual(len(session.calls), 3)
                self.assertEqual(session.calls[2][2]["json"]["sha"], OLD_SHA)

    def test_lost_save_response_is_ambiguous_and_never_retried(self):
        store, session = self.store([
            branch_response(), content_response(), requests.Timeout("Must not echo credentials: " + TOKEN),
        ])
        state = store.load()
        with self.assertRaises(StateError) as raised:
            store.save(state)
        self.assertNotIn(TOKEN, str(raised.exception))
        self.assertIn("may have committed", str(raised.exception))
        with self.assertRaises(StateError):
            store.save(state)
        self.assertEqual(len(session.calls), 3)

    def test_server_error_on_save_does_not_trigger_a_second_write(self):
        store, session = self.store([
            branch_response(), content_response(), FakeResponse(503, {"message": "Unavailable"}),
        ])
        state = store.load()
        with self.assertRaises(StateError):
            store.save(state)
        with self.assertRaises(StateError):
            store.save(state)
        self.assertEqual(len(session.calls), 3)

    def test_success_without_valid_new_sha_still_blocks_the_caller(self):
        for payload in ({}, {"content": {}}, {"content": {"sha": "bad"}}, ValueError("Bad JSON")):
            with self.subTest(payload=repr(payload)):
                store, session = self.store([branch_response(), content_response(), FakeResponse(200, payload)])
                state = store.load()
                with self.assertRaises(StateError):
                    store.save(state)
                with self.assertRaises(StateError):
                    store.save(state)
                self.assertEqual(len(session.calls), 3)

    def test_malformed_or_private_remote_state_fails_closed(self):
        invalid = populated_state()
        invalid["records"][POST_HASH]["text"] = "Private message must not be retained"
        bad_contents = [
            content_response(raw=b"broken JSON"),
            content_response(state=invalid),
            content_response(state={"version": 1}),
            FakeResponse(200, {"type": "file", "encoding": "base64", "sha": OLD_SHA, "content": "%%%"}),
            FakeResponse(200, {"type": "file", "encoding": "none", "sha": OLD_SHA, "content": ""}),
        ]
        for response in bad_contents:
            with self.subTest(payload=repr(response.payload)[:100]):
                store, session = self.store([branch_response(), response])
                with self.assertRaises(StateError):
                    store.load()
                with self.assertRaises(StateError):
                    store.save(initial_state())
                self.assertEqual(len(session.calls), 2)

    def test_invalid_local_state_does_not_issue_a_remote_write(self):
        store, session = self.store([branch_response(), content_response()])
        state = store.load()
        state["records"][POST_HASH]["handle"] = "@must_not_store"
        with self.assertRaises(StateError):
            store.save(state)
        self.assertEqual(len(session.calls), 2)

    def test_save_without_a_successful_load_never_makes_a_request(self):
        store, session = self.store([])
        with self.assertRaises(StateError):
            store.save(initial_state())
        self.assertEqual(session.calls, [])

    def test_redirect_is_not_followed_and_never_bootstraps_state(self):
        store, session = self.store([FakeResponse(302, {"message": "Redirect"})])
        with self.assertRaises(StateError):
            store.load()
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(session.calls[0][2]["allow_redirects"])

    def test_failed_reload_invalidates_old_metadata_for_saving(self):
        store, session = self.store([
            branch_response(), content_response(), branch_response(), FakeResponse(404, {}),
        ])
        state = store.load()
        with self.assertRaises(StateError):
            store.load()
        with self.assertRaises(StateError):
            store.save(state)
        self.assertEqual(len(session.calls), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
