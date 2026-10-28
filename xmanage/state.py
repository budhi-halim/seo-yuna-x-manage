"""Fail-closed local and GitHub-backed engagement state.

The GitHub store performs one request per mutation. Successful writes advance the
Contents API blob SHA; stale or ambiguous writes invalidate the instance. A caller
must wait for save() to return before making the corresponding public X mutation.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import quote

import requests


MAX_STATE_BYTES = 1024 * 1024
API_ROOT = "https://api.github.com"
TOP_FIELDS = {"version", "account_id", "since_id", "records", "opted_out", "last_alert_at"}
RECORD_FIELDS = {"status", "updated_at", "attempted_at", "reply_id", "reason", "author_hash"}
STATUSES = {"ignored", "manual", "replied", "posting", "uncertain", "retry", "blocked"}
HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
DECIMAL_PATTERN = re.compile(r"[0-9]+\Z")
TIMESTAMP_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|\+00:00)\Z"
)


class StateError(Exception):
    """State was unavailable, invalid, or not durably saved; stop the operation."""


def initial_state() -> dict:
    """Return a new empty state, only for a genuinely new store."""
    return {
        "version": 1,
        "account_id": None,
        "since_id": None,
        "records": {},
        "opted_out": {},
        "last_alert_at": None,
    }


def _decimal(value, field: str, *, nullable: bool = True) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not DECIMAL_PATTERN.fullmatch(value):
        raise StateError(f"State {field} must be a decimal string" + (" or null." if nullable else "."))


def _timestamp(value, field: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not TIMESTAMP_PATTERN.fullmatch(value):
        raise StateError(f"State {field} must be an ISO 8601 UTC timestamp" + (" or null." if nullable else "."))
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise StateError(f"State {field} contains an invalid timestamp.") from error
    if parsed.utcoffset() != timedelta(0):
        raise StateError(f"State {field} must use UTC.")


def _hash(value, field: str) -> None:
    if not isinstance(value, str) or not HASH_PATTERN.fullmatch(value):
        raise StateError(f"State {field} must be a lowercase SHA-256 hexadecimal value.")


def validate_state(state: dict) -> None:
    """Reject unknown fields and private/free-text payloads before persistence."""
    if not isinstance(state, dict) or set(state) != TOP_FIELDS:
        raise StateError("State must contain exactly the six documented top-level fields.")
    if type(state["version"]) is not int or state["version"] != 1:
        raise StateError("Unsupported state version; preserve the existing state and use compatible code.")
    _decimal(state["account_id"], "account_id")
    _decimal(state["since_id"], "since_id")
    _timestamp(state["last_alert_at"], "last_alert_at", nullable=True)
    if not isinstance(state["records"], dict) or not isinstance(state["opted_out"], dict):
        raise StateError("State records and opted_out must be JSON objects.")
    for key, record in state["records"].items():
        _hash(key, "record key")
        if not isinstance(record, dict) or not {"status", "updated_at"} <= set(record) or not set(record) <= RECORD_FIELDS:
            raise StateError("A state record has missing or unrecognized fields; free text is not permitted.")
        if not isinstance(record["status"], str) or record["status"] not in STATUSES:
            raise StateError("A state record has an unsupported status.")
        _timestamp(record["updated_at"], "record updated_at")
        if "attempted_at" in record:
            _timestamp(record["attempted_at"], "record attempted_at", nullable=True)
        if "reply_id" in record:
            _decimal(record["reply_id"], "record reply_id")
        if record.get("reason") is not None:
            if not isinstance(record["reason"], str) or not re.fullmatch(r"[a-z_]{1,64}", record["reason"]):
                raise StateError("State record reason must be a documented machine code, not free text.")
        if "author_hash" in record:
            _hash(record["author_hash"], "record author_hash")
    for key, entry in state["opted_out"].items():
        _hash(key, "opt-out key")
        if not isinstance(entry, dict) or set(entry) != {"at", "reason"}:
            raise StateError("An opt-out entry must contain exactly at and reason.")
        _timestamp(entry["at"], "opt-out at")
        if entry["reason"] not in ("user_request", "configured"):
            raise StateError("An opt-out entry has an unsupported reason.")


def _encode(state: dict) -> bytes:
    validate_state(state)
    try:
        raw = (json.dumps(state, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise StateError("State could not be encoded as JSON.") from error
    if len(raw) > MAX_STATE_BYTES:
        raise StateError("State exceeds the 1 MiB limit; preserve it and arrange a supported migration before continuing.")
    return raw


def _unique_object(pairs) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Non-JSON numeric constant")


def _decode(raw: bytes) -> dict:
    if len(raw) > MAX_STATE_BYTES:
        raise StateError("State exceeds the supported 1 MiB limit; it has not been reset.")
    try:
        state = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                           parse_constant=_invalid_constant)
    except (UnicodeError, ValueError, TypeError) as error:
        raise StateError("State is not valid UTF-8 JSON; preserve and repair it before continuing.") from error
    validate_state(state)
    return state


class LocalStateStore:
    """A local state file, replaced atomically only after validation and fsync."""

    def __init__(self, path):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(MAX_STATE_BYTES + 1)
        except FileNotFoundError:
            return initial_state()
        except OSError as error:
            raise StateError("The local state file could not be read; it has not been reset.") from error
        return _decode(raw)

    def save(self, state: dict) -> None:
        raw = _encode(state)
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
            if os.name == "posix":
                directory = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except OSError as error:
            raise StateError("Local state save was not confirmed. Stop before performing the X action.") from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


def _git_sha(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", value):
        raise StateError("GitHub returned an invalid Git object SHA; state has not been trusted.")
    return value.lower()


class GitHubStateStore:
    """Contents API state with blob-SHA compare-and-swap and no write retries.

    The supplied session, when present, must be dedicated to this store. Its auth,
    headers, cookies, environment authentication and GitHub adapter are configured
    so that only the GitHub token is used for the fixed GitHub API host.
    """

    def __init__(self, repo, token, branch="bot-state", path="state/engagement.json", base_sha=None, session=None):
        if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise StateError("GitHub repository must have the form owner/repository.")
        if any(part in (".", "..") for part in repo.split("/")):
            raise StateError("Invalid GitHub repository name.")
        if not isinstance(token, str) or not token.strip() or any(ord(character) < 32 for character in token.strip()):
            raise StateError("A valid GitHub token is required for remote state.")
        for value, label in ((branch, "branch"), (path, "path")):
            if (not isinstance(value, str) or not value or value != value.strip() or "\\" in value
                    or any(ord(character) < 32 for character in value)
                    or any(part in ("", ".", "..") for part in value.split("/"))):
                raise StateError(f"Invalid GitHub state {label}.")
        self.repo, self.branch, self.path = repo, branch, path
        self.base_sha = _git_sha(base_sha) if base_sha else None
        self._token = token.strip()
        self._blob_sha = None
        self._bootstrap_base = None
        self._loaded = False
        self._poisoned = False
        self._headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "SeoYunaXManage-State/1",
        }
        self.session = session if session is not None else requests.Session()
        self.session.auth = None
        self.session.trust_env = False
        if hasattr(self.session, "headers"):
            self.session.headers.clear()
            self.session.headers.update(self._headers)
        if hasattr(self.session, "cookies"):
            self.session.cookies.clear()
        if isinstance(self.session, requests.Session):
            self.session.mount(API_ROOT + "/", requests.adapters.HTTPAdapter(max_retries=0))

    def _usable(self) -> None:
        if self._poisoned:
            raise StateError("An earlier GitHub write was not confirmed or conflicted. Stop; inspect remote state and use a fresh store instance.")

    def _send(self, method, endpoint, **kwargs):
        self._usable()
        try:
            return self.session.request(method, API_ROOT + endpoint, headers=dict(self._headers),
                                        timeout=(10, 30), allow_redirects=False, **kwargs)
        except requests.RequestException:
            if method in ("POST", "PUT", "PATCH", "DELETE"):
                self._poisoned = True
                raise StateError("GitHub write response was lost; it may have committed. No automatic retry is permitted. Stop before the X action.") from None
            raise StateError("GitHub state could not be read because the request failed; no empty state was substituted.") from None

    @staticmethod
    def _object(response) -> dict:
        try:
            value = response.json()
        except (ValueError, TypeError) as error:
            raise StateError("GitHub returned unreadable JSON; state could not be confirmed.") from error
        if not isinstance(value, dict):
            raise StateError("GitHub returned an unexpected response shape; state could not be confirmed.")
        return value

    def _http_error(self, response, operation) -> StateError:
        detail = ""
        try:
            payload = response.json()
            if isinstance(payload, dict) and isinstance(payload.get("message"), str):
                detail = payload["message"].replace(self._token, "[REDACTED]")[:300]
        except (ValueError, TypeError):
            pass
        suffix = f" {detail}" if detail else ""
        return StateError(f"GitHub {operation} failed (HTTP {response.status_code}). No state was reset or overwritten.{suffix}")

    def _branch_endpoint(self, branch) -> str:
        return f"/repos/{self.repo}/git/ref/heads/{quote(branch, safe='')}"

    def _contents_endpoint(self) -> str:
        return f"/repos/{self.repo}/contents/{quote(self.path, safe='/')}"

    def _base_commit(self) -> str:
        if self.base_sha:
            return self.base_sha
        environment_sha = os.environ.get("GITHUB_SHA", "").strip()
        if environment_sha:
            return _git_sha(environment_sha)
        response = self._send("GET", f"/repos/{self.repo}")
        if response.status_code != 200:
            raise self._http_error(response, "repository lookup")
        default_branch = self._object(response).get("default_branch")
        if not isinstance(default_branch, str) or not default_branch:
            raise StateError("GitHub did not identify the repository's default branch.")
        response = self._send("GET", self._branch_endpoint(default_branch))
        if response.status_code != 200:
            raise self._http_error(response, "default-branch lookup")
        payload = self._object(response)
        if payload.get("ref") != f"refs/heads/{default_branch}" or not isinstance(payload.get("object"), dict):
            raise StateError("GitHub returned an unexpected default-branch reference.")
        return _git_sha(payload["object"].get("sha"))

    def _branch_exists(self) -> bool:
        """Read-only lookup; a missing branch is bootstrapped only by save()."""
        response = self._send("GET", self._branch_endpoint(self.branch))
        if response.status_code == 200:
            payload = self._object(response)
            if payload.get("ref") != f"refs/heads/{self.branch}" or not isinstance(payload.get("object"), dict):
                raise StateError("GitHub returned an unexpected state-branch reference.")
            _git_sha(payload["object"].get("sha"))
            return True
        if response.status_code != 404:
            raise self._http_error(response, "state-branch lookup")
        return False

    def _create_branch(self, base: str) -> None:
        """Create once; an already-created branch is a conflict, never a retry."""
        try:
            response = self._send("POST", f"/repos/{self.repo}/git/refs",
                                  json={"ref": f"refs/heads/{self.branch}", "sha": base})
            if response.status_code != 201:
                raise self._http_error(response, "state-branch creation")
            payload = self._object(response)
            if payload.get("ref") != f"refs/heads/{self.branch}" or not isinstance(payload.get("object"), dict):
                raise StateError("GitHub branch creation was not confirmed; inspect the branch before continuing.")
            if _git_sha(payload["object"].get("sha")) != base:
                raise StateError("GitHub created a branch at an unexpected base; inspect it before continuing.")
        except StateError:
            self._poisoned = True
            raise

    def load(self) -> dict:
        self._usable()
        # A failed reload must not leave older metadata usable for a later save.
        self._loaded = False
        self._blob_sha = None
        self._bootstrap_base = None
        if not self._branch_exists():
            self._bootstrap_base = self._base_commit()
            self._loaded = True
            return initial_state()
        response = self._send("GET", self._contents_endpoint(), params={"ref": self.branch})
        if response.status_code == 404:
            raise StateError("The existing state branch is missing its state file. It was not reset. Inspect or restore the file before continuing.")
        if response.status_code != 200:
            raise self._http_error(response, "state-file read")
        payload = self._object(response)
        if payload.get("type") != "file" or payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
            raise StateError("GitHub state is not a readable Contents API file; check its type, encoding and 1 MiB limit.")
        sha = _git_sha(payload.get("sha"))
        content = payload["content"]
        if len(content) > (MAX_STATE_BYTES * 4 // 3) + 65536:
            raise StateError("GitHub state exceeds the supported size limit; it has not been reset.")
        try:
            raw = base64.b64decode("".join(content.split()), validate=True)
        except (binascii.Error, ValueError, TypeError) as error:
            raise StateError("GitHub state contains invalid base64; it has not been reset.") from error
        state = _decode(raw)
        self._blob_sha = sha
        self._loaded = True
        return state

    def save(self, state: dict) -> None:
        self._usable()
        if not self._loaded:
            raise StateError("Load and validate remote state before attempting to save it.")
        raw = _encode(state)
        body = {
            "message": "Update engagement state",
            "content": base64.b64encode(raw).decode("ascii"),
            "branch": self.branch,
        }
        if self._blob_sha is not None:
            body["sha"] = self._blob_sha
        try:
            if self._bootstrap_base is not None:
                self._create_branch(self._bootstrap_base)
            response = self._send("PUT", self._contents_endpoint(), json=body)
            expected_status = 200 if self._blob_sha is not None else 201
            if response.status_code != expected_status:
                raise self._http_error(response, "state save (compare-and-swap)")
            payload = self._object(response)
            content = payload.get("content")
            if not isinstance(content, dict):
                raise StateError("GitHub accepted the write but did not confirm its content SHA. Stop before the X action.")
            next_sha = _git_sha(content.get("sha"))
        except StateError:
            self._poisoned = True
            raise
        self._blob_sha = next_sha
        self._bootstrap_base = None

    def close(self) -> None:
        self.session.close()
