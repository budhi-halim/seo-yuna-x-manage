"""Small OAuth1 client for the documented X v2.172 Post field schema."""
from dataclasses import dataclass, field
import time

import requests
from requests_oauthlib import OAuth1

from .errors import ApiError, IncompleteTimeline, ManagerError

API_ROOT = "https://api.x.com/2"
POST_FIELDS = "id,text,created_at,conversation_id,entities,lang,possibly_sensitive,attachments"
EXPANSIONS = "author_id,in_reply_to_user_id,referenced_posts"
USER_FIELDS = "id,username,name"


@dataclass
class Timeline:
    posts: list = field(default_factory=list)
    parents: dict = field(default_factory=dict)
    users: dict = field(default_factory=dict)


def decode_collection(payload, *, single=False):
    if not single and "data" not in payload:
        meta = payload.get("meta")
        if not isinstance(meta, dict) or type(meta.get("result_count")) is not int or meta["result_count"] != 0:
            raise ApiError("X omitted post data without confirming an empty result.")
    data = payload.get("data", [] if not single else None)
    if single:
        data = [data] if isinstance(data, dict) else []
    if not isinstance(data, list):
        raise ApiError("X returned an invalid post collection.")
    for post in data:
        if not isinstance(post, dict) or not str(post.get("id", "")).isdecimal():
            raise ApiError("X returned an invalid Post ID.")
        post["id"] = str(post["id"])
    includes = payload.get("includes") or {}
    if not isinstance(includes, dict):
        raise ApiError("X returned invalid expansion data.")
    parents = includes.get("posts", includes.get("tweets", [])) or []
    users = includes.get("users") or []
    if not isinstance(parents, list) or not isinstance(users, list):
        raise ApiError("X returned invalid expanded posts/users.")
    return Timeline(data, {str(p["id"]): p for p in parents if isinstance(p, dict) and p.get("id")},
                    {str(p["id"]): p for p in users if isinstance(p, dict) and p.get("id")})


class XClient:
    def __init__(self, credentials, request_budget=60, *, session=None, sleep_fn=time.sleep):
        self.session = session or requests.Session()
        self.session.auth = OAuth1(credentials["X_API_KEY"], credentials["X_API_KEY_SECRET"],
                                   credentials["X_ACCESS_TOKEN"], credentials["X_ACCESS_TOKEN_SECRET"])
        self.session.headers.update({"Accept": "application/json", "User-Agent": "SeoYunaXManager/1.0"})
        self.request_budget = request_budget
        self.requests_used = 0
        self.sleep_fn = sleep_fn

    def close(self):
        self.session.close()

    def _request(self, method, endpoint, *, publishing=False, **kwargs):
        for attempt in range(3 if method == "GET" else 1):
            if self.requests_used >= self.request_budget:
                raise ApiError("Per-run API request budget exhausted. Resume in a later run.")
            self.requests_used += 1
            try:
                response = self.session.request(method, API_ROOT + endpoint, timeout=(15, 60),
                                                allow_redirects=False, **kwargs)
            except requests.RequestException as error:
                if not publishing and attempt < 2:
                    self.sleep_fn(2 ** (attempt + 1))
                    continue
                raise ApiError("X connection failed; no raw request details were logged.",
                               ambiguous=publishing) from error
            status = response.status_code
            if method == "GET" and status >= 500 and attempt < 2:
                self.sleep_fn(2 ** (attempt + 1))
                continue
            if not 200 <= status < 300:
                hints = {
                    400: "Request rejected. Check current API field schema and configured reply tokens.",
                    401: "Authentication failed. Check all four credentials and app permission.",
                    402: "API credits or billing need attention.",
                    403: "Request forbidden. Check app access, reply eligibility and account restrictions.",
                    404: "The requested resource is unavailable.",
                    429: "Rate limit reached. This run stops; resume after the limit resets.",
                }
                raise ApiError(f"X HTTP {status}: " + hints.get(status, "API request failed."), status,
                               ambiguous=publishing and (status >= 500 or status in {408, 425}))
            try:
                payload = response.json()
            except (ValueError, TypeError) as error:
                raise ApiError("X returned unreadable JSON.", status, ambiguous=publishing) from error
            if not isinstance(payload, dict):
                raise ApiError("X returned an unexpected response shape.", status, ambiguous=publishing)
            if payload.get("errors"):
                raise ApiError("X returned partial/error data. No incomplete scan will be treated as complete.",
                               status, ambiguous=publishing)
            return payload
        raise ApiError("X read retry budget exhausted.")

    def me(self):
        payload = self._request("GET", "/users/me")
        account = payload.get("data")
        if not isinstance(account, dict) or not str(account.get("id", "")).isdecimal() or not account.get("username"):
            raise ApiError("Authenticated X account could not be identified.")
        return {"id": str(account["id"]), "username": str(account["username"])}

    def timeline(self, kind, user_id, *, since_id=None, start_time=None, page_size=100, max_pages=5):
        if kind not in {"mentions", "tweets"}:
            raise ValueError("Unsupported timeline kind")
        if not str(user_id).isdecimal():
            raise ValueError("Invalid user ID")
        params = {"max_results": page_size, "post.fields": POST_FIELDS,
                  "expansions": EXPANSIONS, "user.fields": USER_FIELDS}
        if kind == "tweets":
            params["exclude"] = "retweets"  # Keep replies for manual-answer detection.
        if since_id:
            params["since_id"] = str(since_id)
        elif start_time:
            params["start_time"] = start_time
        result = Timeline()
        seen_posts, seen_tokens = set(), set()
        for _ in range(max_pages):
            payload = self._request("GET", f"/users/{user_id}/{kind}", params=dict(params))
            page = decode_collection(payload)
            for post in page.posts:
                if post["id"] not in seen_posts:
                    result.posts.append(post)
                    seen_posts.add(post["id"])
            result.parents.update(page.parents)
            result.users.update(page.users)
            meta = payload.get("meta") or {}
            if not isinstance(meta, dict):
                raise ApiError("X returned invalid pagination metadata.")
            token = meta.get("next_token")
            if not token:
                # API visibility caps are not evidence of a complete older inbox.
                hard_cap = 800 if kind == "mentions" else 3200
                if len(result.posts) >= hard_cap:
                    raise IncompleteTimeline(f"The {kind} visibility cap was reached. Review the backlog before replying.")
                return result
            if not isinstance(token, str) or token in seen_tokens:
                raise IncompleteTimeline("X returned repeated or invalid pagination. Cursor was not advanced.")
            seen_tokens.add(token)
            params["pagination_token"] = token
        raise IncompleteTimeline(f"The {kind} page budget was reached. No partial scan is used for replies.")

    def post(self, post_id):
        if not str(post_id).isdecimal():
            raise ValueError("Invalid Post ID")
        payload = self._request("GET", f"/tweets/{post_id}",
                                params={"post.fields": POST_FIELDS, "expansions": EXPANSIONS,
                                        "user.fields": USER_FIELDS})
        result = decode_collection(payload, single=True)
        if len(result.posts) != 1 or result.posts[0]["id"] != str(post_id):
            raise ApiError("X did not return the requested post.")
        return result

    def reply(self, text, source_post_id):
        payload = self._request("POST", "/tweets", publishing=True,
                                json={"text": text, "reply": {"in_reply_to_tweet_id": str(source_post_id)}})
        data = payload.get("data")
        if not isinstance(data, dict) or not str(data.get("id", "")).isdecimal():
            raise ApiError("X accepted a reply request without a verifiable result ID.", ambiguous=True)
        return str(data["id"])
