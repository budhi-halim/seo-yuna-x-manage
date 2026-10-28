"""Offline API contract and duplicate-prevention checks; never contacts X."""
import copy
import unittest

import requests

from xmanage.client import API_ROOT, XClient, decode_collection
from xmanage.errors import ApiError, IncompleteTimeline, ManagerError


FAKE_CREDENTIALS = {
    "X_API_KEY": "fake-key", "X_API_KEY_SECRET": "fake-key-secret",
    "X_ACCESS_TOKEN": "fake-token", "X_ACCESS_TOKEN_SECRET": "fake-token-secret",
}


class FakeResponse:
    def __init__(self, status=200, payload=None, json_error=None):
        self.status_code = status
        self.payload = payload
        self.json_error = json_error

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return copy.deepcopy(self.payload)


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}
        self.auth = None
        self.closed = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, copy.deepcopy(kwargs)))
        if not self.responses:
            raise AssertionError("Unexpected API call; no real transport is available.")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def close(self):
        self.closed = True


def page(ids=(), *, next_token=None, includes=None):
    result = {"data": [{"id": str(identifier), "text": "hello"} for identifier in ids],
              "meta": {"result_count": len(ids)}}
    if next_token is not None:
        result["meta"]["next_token"] = next_token
    if includes is not None:
        result["includes"] = includes
    return result


class ClientTests(unittest.TestCase):
    def make_client(self, *responses, budget=60):
        self.session = FakeSession(*responses)
        self.delays = []
        return XClient(FAKE_CREDENTIALS, request_budget=budget,
                       session=self.session, sleep_fn=self.delays.append)

    def test_timeline_and_lookup_use_current_field_schema(self):
        """Current API reads use post.fields; writes still use in_reply_to_tweet_id."""
        client = self.make_client(FakeResponse(payload=page()), FakeResponse(payload=page()),
                                  FakeResponse(payload={"data": {"id": "90"}}))
        client.timeline("mentions", "10", since_id="80")
        client.timeline("tweets", "10", start_time="2026-10-28T00:00:00Z")
        client.post("90")
        valid_fields = {
            "id", "text", "created_at", "conversation_id", "entities", "lang",
            "possibly_sensitive", "attachments",
        }
        for method, url, kwargs in self.session.calls:
            self.assertEqual(method, "GET")
            self.assertTrue(url.startswith(API_ROOT + "/"))
            self.assertFalse(kwargs["allow_redirects"])
            self.assertEqual(kwargs["timeout"], (15, 60))
            params = kwargs["params"]
            self.assertNotIn("tweet.fields", params)
            self.assertEqual(set(params["post.fields"].split(",")), valid_fields)
            self.assertEqual(set(params["expansions"].split(",")),
                             {"author_id", "in_reply_to_user_id", "referenced_posts"})
            self.assertEqual(set(params["user.fields"].split(",")), {"id", "username", "name"})
        self.assertNotIn("exclude", self.session.calls[0][2]["params"])
        self.assertEqual(self.session.calls[1][2]["params"]["exclude"], "retweets")
        self.assertEqual(self.session.calls[0][2]["params"]["since_id"], "80")

    def test_reply_json_keeps_documented_legacy_id_key(self):
        client = self.make_client(FakeResponse(201, {"data": {"id": "999"}}))
        self.assertEqual(client.reply("😊💛", "456"), "999")
        method, url, kwargs = self.session.calls[0]
        self.assertEqual((method, url), ("POST", API_ROOT + "/tweets"))
        self.assertEqual(kwargs["json"], {"text": "😊💛", "reply": {"in_reply_to_tweet_id": "456"}})

    def test_get_retries_transport_and_server_failures_then_succeeds(self):
        client = self.make_client(requests.Timeout("untrusted details"), FakeResponse(503),
                                  FakeResponse(payload={"data": {"id": "10", "username": "yuna"}}))
        self.assertEqual(client.me(), {"id": "10", "username": "yuna"})
        self.assertEqual(client.requests_used, 3)
        self.assertEqual(self.delays, [2, 4])

    def test_get_retries_have_three_attempt_ceiling(self):
        client = self.make_client(*(FakeResponse(503) for _ in range(4)))
        with self.assertRaises(ApiError) as caught:
            client.me()
        self.assertEqual(caught.exception.status, 503)
        self.assertFalse(caught.exception.ambiguous)
        self.assertEqual(len(self.session.calls), 3)

    def test_retry_attempts_cannot_exceed_total_request_budget(self):
        client = self.make_client(FakeResponse(503), FakeResponse(503), FakeResponse(200), budget=2)
        with self.assertRaisesRegex(ManagerError, "budget exhausted"):
            client.me()
        self.assertEqual(client.requests_used, 2)
        self.assertEqual(len(self.session.calls), 2)

    def test_explicit_429_is_not_an_ambiguous_write_or_retried(self):
        client = self.make_client(FakeResponse(429, {"detail": "private body"}))
        with self.assertRaises(ApiError) as caught:
            client.reply("😊", "456")
        self.assertEqual(caught.exception.status, 429)
        self.assertFalse(caught.exception.ambiguous)
        self.assertEqual(len(self.session.calls), 1)
        self.assertEqual(self.delays, [])

    def test_get_429_stops_without_retry(self):
        client = self.make_client(FakeResponse(429))
        with self.assertRaises(ApiError) as caught:
            client.me()
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(len(self.session.calls), 1)

    def test_ambiguous_post_outcomes_are_never_retried(self):
        outcomes = [
            requests.Timeout("private network details"),
            requests.ConnectionError("private network details"),
            FakeResponse(500), FakeResponse(502), FakeResponse(408), FakeResponse(425),
            FakeResponse(201, json_error=ValueError("private invalid JSON")),
            FakeResponse(201, ["unexpected shape"]),
            FakeResponse(201, {}),
            FakeResponse(201, {"data": {"id": "not-an-id"}}),
            FakeResponse(201, {"data": {"id": "999"}, "errors": [{"detail": "partial"}]}),
        ]
        for outcome in outcomes:
            with self.subTest(outcome=type(outcome).__name__, status=getattr(outcome, "status_code", None)):
                client = self.make_client(outcome)
                with self.assertRaises(ApiError) as caught:
                    client.reply("😊", "456")
                self.assertTrue(caught.exception.ambiguous)
                self.assertEqual(len(self.session.calls), 1)
                self.assertEqual(self.delays, [])

    def test_api_error_details_are_not_exposed_to_operator(self):
        secret_marker = "NEVER_PRINT_FAKE_CREDENTIAL"
        for response in (
            FakeResponse(401, {"detail": secret_marker}),
            FakeResponse(403, {"detail": secret_marker}),
            FakeResponse(200, {"data": [], "errors": [{"detail": secret_marker}]}),
        ):
            with self.subTest(status=response.status_code):
                client = self.make_client(response)
                with self.assertRaises(ApiError) as caught:
                    client.timeline("mentions", "10")
                self.assertNotIn(secret_marker, str(caught.exception))

    def test_partial_data_is_not_a_completed_scan(self):
        client = self.make_client(FakeResponse(payload={
            "data": [{"id": "1"}], "errors": [{"title": "Unavailable expansion"}],
            "meta": {"result_count": 1},
        }))
        with self.assertRaisesRegex(ApiError, "partial/error"):
            client.timeline("mentions", "10")

    def test_pagination_merges_pages_and_expansions_without_duplicate_posts(self):
        first = page([11, 12], next_token="next", includes={"posts": [{"id": "1", "author_id": "10"}],
                                                          "users": [{"id": "20", "username": "reader"}]})
        second = page([12, 13], includes={"posts": [{"id": "2", "author_id": "10"}]})
        client = self.make_client(FakeResponse(payload=first), FakeResponse(payload=second))
        result = client.timeline("mentions", "10", since_id="9")
        self.assertEqual([post["id"] for post in result.posts], ["11", "12", "13"])
        self.assertEqual(set(result.parents), {"1", "2"})
        self.assertEqual(result.users["20"]["username"], "reader")
        self.assertNotIn("pagination_token", self.session.calls[0][2]["params"])
        self.assertEqual(self.session.calls[1][2]["params"]["pagination_token"], "next")
        self.assertEqual(self.session.calls[1][2]["params"]["since_id"], "9")

    def test_next_page_failure_does_not_return_first_page(self):
        client = self.make_client(FakeResponse(payload=page([11], next_token="next")), FakeResponse(403))
        with self.assertRaises(ApiError):
            client.timeline("tweets", "10")

    def test_pagination_repeated_or_non_string_token_is_incomplete(self):
        for pages in (
            [page([11], next_token="same"), page([12], next_token="same")],
            [page([11], next_token=123)],
        ):
            with self.subTest(pages=pages):
                client = self.make_client(*(FakeResponse(payload=item) for item in pages))
                with self.assertRaises(IncompleteTimeline):
                    client.timeline("mentions", "10")

    def test_page_cap_does_not_turn_backlog_into_complete_scan(self):
        client = self.make_client(FakeResponse(payload=page([11], next_token="next")))
        with self.assertRaises(IncompleteTimeline):
            client.timeline("mentions", "10", max_pages=1)

    def test_mention_visibility_cap_does_not_imply_complete_history(self):
        pages = [page(range(1 + 100 * i, 101 + 100 * i), next_token=str(i + 1) if i < 7 else None)
                 for i in range(8)]
        client = self.make_client(*(FakeResponse(payload=item) for item in pages))
        with self.assertRaisesRegex(IncompleteTimeline, "visibility cap"):
            client.timeline("mentions", "10", max_pages=8)

    def test_malformed_success_cannot_be_empty_manual_reply_timeline(self):
        for payload in ({}, {"meta": {}}, {"data": [] , "meta": ["invalid"]}):
            with self.subTest(payload=payload):
                client = self.make_client(FakeResponse(payload=payload))
                with self.assertRaises(ApiError):
                    client.timeline("tweets", "10")

    def test_documented_empty_result_without_data_is_valid(self):
        client = self.make_client(FakeResponse(payload={"meta": {"result_count": 0}}))
        self.assertEqual(client.timeline("tweets", "10").posts, [])

    def test_lookup_must_return_requested_id(self):
        client = self.make_client(FakeResponse(payload={"data": {"id": "999"}}))
        with self.assertRaisesRegex(ApiError, "requested post"):
            client.post("123")

    def test_current_and_legacy_expanded_post_keys_decode(self):
        for field in ("posts", "tweets"):
            with self.subTest(field=field):
                result = decode_collection({"data": [{"id": "22"}], "includes": {
                    field: [{"id": "11", "author_id": "10"}],
                    "users": [{"id": "20", "username": "reader"}],
                }})
                self.assertEqual(result.parents["11"]["author_id"], "10")
                self.assertEqual(result.users["20"]["username"], "reader")


if __name__ == "__main__":
    unittest.main()
