"""Deterministic eligibility checks; no AI, auto-likes, or unsolicited discovery."""
import hashlib
import re
import unicodedata


def item_hash(kind, identifier):
    return hashlib.sha256(f"{kind}:{identifier}".encode("utf-8")).hexdigest()


def references(post):
    value = post.get("referenced_posts", post.get("referenced_tweets", [])) or []
    return value if isinstance(value, list) else []


def reply_parent_id(post):
    ids = [str(ref.get("id", "")) for ref in references(post)
           if isinstance(ref, dict) and ref.get("type") == "replied_to"]
    return ids[0] if len(ids) == 1 and ids[0].isdecimal() else None


def mentions_owner(post, account):
    entities = post.get("entities") or {}
    if not isinstance(entities, dict):
        return False
    for mention in entities.get("mentions", []) or []:
        if not isinstance(mention, dict):
            continue
        if mention.get("id") is not None:
            if str(mention["id"]) == str(account["id"]):
                return True
        elif str(mention.get("username", "")).casefold() == account["username"].casefold():
            return True
    return False


def without_own_mention(text, username):
    value = unicodedata.normalize("NFKC", str(text))
    return re.sub(r"(?<![\w@])@" + re.escape(username) + r"(?![A-Za-z0-9_])", "", value,
                  flags=re.IGNORECASE).strip()


OPT_OUT_PHRASES = {
    "stop", "unsubscribe", "opt out", "opt-out", "stop bot", "stop replies",
    "no replies", "don't reply", "dont reply", "do not reply", "stop replying",
    "berhenti", "jangan balas", "jangan dibalas",
}


def is_opt_out(post, account):
    # Called only on incoming mentions fetched for this account; no response is sent.
    body = without_own_mention(post.get("text", ""), account["username"]).casefold()
    body = " ".join(body.split()).strip(" .,!?:;\"'()[]")
    return body in OPT_OUT_PHRASES and str(post.get("author_id", "")) != str(account["id"])


class ContentPolicy:
    def __init__(self, words, max_length=100):
        self.max_length = max_length
        self.patterns = []
        for word in words:
            word = unicodedata.normalize("NFKC", word).casefold()
            asian = any(0x0E00 <= ord(c) <= 0x0E7F or 0x3040 <= ord(c) <= 0x30FF or
                        0x4E00 <= ord(c) <= 0x9FFF or 0xAC00 <= ord(c) <= 0xD7A3 for c in word)
            if asian:
                self.patterns.append(("substring", word))
            else:
                left = r"(?<!\w)" if re.match(r"\w", word) else ""
                right = r"(?!\w)" if re.search(r"\w$", word) else ""
                self.patterns.append(("regex", re.compile(left + re.escape(word) + right)))

    def matches_filter(self, value):
        value = unicodedata.normalize("NFKC", value).casefold()
        return any((pattern in value) if kind == "substring" else bool(pattern.search(value))
                   for kind, pattern in self.patterns)

    def evaluate(self, post, account, parents, users, opted_out):
        author_id = str(post.get("author_id", ""))
        if not author_id.isdecimal():
            return "missing_author"
        if author_id == str(account["id"]):
            return "self"
        if item_hash("user", author_id) in opted_out:
            return "opted_out"
        parent_id = reply_parent_id(post)
        parent = parents.get(parent_id, {})
        if not parent_id or str(parent.get("author_id", "")) != str(account["id"]):
            return "not_direct_reply"
        if str(post.get("in_reply_to_user_id", "")) != str(account["id"]):
            return "not_direct_reply"
        # Do not respond to replies to our own replies: avoids acknowledgement loops.
        if reply_parent_id(parent):
            return "nested_reply"
        if not mentions_owner(post, account):
            return "missing_explicit_mention"
        if post.get("possibly_sensitive") or post.get("attachments"):
            return "media_or_sensitive"
        body = without_own_mention(post.get("text", ""), account["username"])
        if not body or len(body) > self.max_length:
            return "length"
        if any(character in body for character in ("?", "؟", "¿")):
            return "question"
        if "@" in body:
            return "other_mention"
        if "http" in body.casefold() or "www." in body.casefold() or (post.get("entities") or {}).get("urls"):
            return "url"
        if self.matches_filter(body):
            return "filtered"
        author = users.get(author_id)
        if not isinstance(author, dict):
            return "missing_author_profile"
        if self.matches_filter(str(author.get("name", "")) + " " + str(author.get("username", ""))):
            return "filtered_author"
        return None
