"""Offline eligibility checks for public replies and opt-out handling."""
import copy
import unittest

from xmanage.policy import (ContentPolicy, is_opt_out, item_hash, mentions_owner,
                            references, reply_parent_id, without_own_mention)


ACCOUNT = {"id": "10", "username": "SeoYuna"}
PARENTS = {"100": {"id": "100", "author_id": "10", "text": "An original post"}}
USERS = {"20": {"id": "20", "username": "reader", "name": "Reader"}}


def incoming(**overrides):
    post = {"id": "200", "author_id": "20", "in_reply_to_user_id": "10",
            "text": "@SeoYuna Beautiful photo!", "possibly_sensitive": False,
            "entities": {"mentions": [{"id": "10", "username": "SeoYuna"}]},
            "referenced_posts": [{"type": "replied_to", "id": "100"}]}
    post.update(overrides)
    return post


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = ContentPolicy(["spam", "scam", "骗局", "詐欺", "사기", "โกง"], max_length=100)

    def evaluate(self, post=None, *, parents=None, users=None, opted_out=None):
        return self.policy.evaluate(incoming() if post is None else post, ACCOUNT,
                                    PARENTS if parents is None else parents,
                                    USERS if users is None else users,
                                    set() if opted_out is None else opted_out)

    def test_own_direct_reply_with_explicit_mention_is_eligible(self):
        self.assertIsNone(self.evaluate())

    def test_numeric_mention_id_is_authoritative(self):
        post = incoming(entities={"mentions": [{"id": "10", "username": "old_handle"}]})
        self.assertTrue(mentions_owner(post, ACCOUNT))
        post = incoming(entities={"mentions": [{"id": "999", "username": "SeoYuna"}]})
        self.assertFalse(mentions_owner(post, ACCOUNT))
        self.assertEqual(self.evaluate(post), "missing_explicit_mention")

    def test_username_mention_fallback_requires_absent_id(self):
        for mention in ({"username": "sEOyUNA"}, {"id": None, "username": "SEOYUNA"}):
            with self.subTest(mention=mention):
                self.assertIsNone(self.evaluate(incoming(entities={"mentions": [mention]})))
        self.assertFalse(mentions_owner(incoming(entities={"mentions": [{}]}), ACCOUNT))

    def test_textual_mention_alone_is_not_eligibility_evidence(self):
        self.assertEqual(self.evaluate(incoming(entities={})), "missing_explicit_mention")

    def test_parent_must_be_known_and_authored_by_owner(self):
        for parents in ({}, {"100": {"id": "100", "author_id": "99"}}):
            with self.subTest(parents=parents):
                self.assertEqual(self.evaluate(parents=parents), "not_direct_reply")
        self.assertEqual(self.evaluate(incoming(in_reply_to_user_id="99")), "not_direct_reply")
        self.assertEqual(self.evaluate(incoming(in_reply_to_user_id=None)), "not_direct_reply")

    def test_quote_or_ordinary_mention_is_not_direct_reply(self):
        for refs in ([], [{"type": "quoted", "id": "100"}]):
            with self.subTest(refs=refs):
                self.assertEqual(self.evaluate(incoming(referenced_posts=refs)), "not_direct_reply")

    def test_replies_to_owners_replies_are_excluded(self):
        parents = copy.deepcopy(PARENTS)
        parents["100"]["referenced_posts"] = [{"type": "replied_to", "id": "99"}]
        self.assertEqual(self.evaluate(parents=parents), "nested_reply")

    def test_reference_aliases_keep_same_reply_parent(self):
        for key in ("referenced_posts", "referenced_tweets"):
            with self.subTest(key=key):
                post = incoming()
                refs = post.pop("referenced_posts")
                post[key] = refs
                self.assertEqual(references(post), refs)
                self.assertEqual(reply_parent_id(post), "100")
                self.assertIsNone(self.evaluate(post))

    def test_multiple_reply_parents_are_not_trusted(self):
        self.assertIsNone(reply_parent_id({"referenced_posts": [
            {"type": "replied_to", "id": "1"}, {"type": "replied_to", "id": "2"}]}))
        self.assertIsNone(reply_parent_id({"referenced_posts": [{"type": "replied_to", "id": "bad"}]}))

    def test_remove_only_exact_own_mentions(self):
        self.assertEqual(without_own_mention("@SEOYUNA Thanks @reader", "SeoYuna"), "Thanks @reader")
        self.assertEqual(without_own_mention("@SeoYuna2 Thanks", "SeoYuna"), "@SeoYuna2 Thanks")
        self.assertEqual(without_own_mention("@@SeoYuna Thanks", "SeoYuna"), "@@SeoYuna Thanks")
        self.assertEqual(self.evaluate(incoming(text="@SeoYuna Thanks @reader")), "other_mention")
        self.assertEqual(self.evaluate(incoming(text="@SeoYuna Thanks ＠reader")), "other_mention")

    def test_own_mention_does_not_count_toward_body_length(self):
        self.assertIsNone(self.evaluate(incoming(text="@SeoYuna " + "a" * 100)))
        self.assertEqual(self.evaluate(incoming(text="@SeoYuna " + "a" * 101)), "length")
        self.assertEqual(self.evaluate(incoming(text="@SeoYuna")), "length")

    def test_filtered_words_use_boundaries_and_unicode_normalization(self):
        for value in ("spam", "SPAM!", "This is spam.", "ｓｐａｍ", "a scam here"):
            with self.subTest(value=value):
                self.assertTrue(self.policy.matches_filter(value))
        for value in ("spamalot", "scamper", "notspam", "spammy"):
            with self.subTest(value=value):
                self.assertFalse(self.policy.matches_filter(value))

    def test_asian_filters_match_inside_unspaced_text(self):
        for value in ("这是一个骗局啊", "これは詐欺です", "정말사기네요", "นี่มันโกงมาก"):
            with self.subTest(value=value):
                self.assertTrue(self.policy.matches_filter(value))
                self.assertEqual(self.evaluate(incoming(text="@SeoYuna " + value)), "filtered")

    def test_questions_in_common_scripts_are_skipped(self):
        for value in ("Nice?", "Nice？", "جميل؟", "¿Bonita"):
            with self.subTest(value=value):
                self.assertEqual(self.evaluate(incoming(text="@SeoYuna " + value)), "question")

    def test_urls_in_text_or_entities_are_skipped(self):
        for value in ("https://example.test", "HTTP://example.test", "www.example.test"):
            with self.subTest(value=value):
                self.assertEqual(self.evaluate(incoming(text="@SeoYuna " + value)), "url")
        post = incoming()
        post["entities"]["urls"] = [{"url": "https://t.co/example"}]
        self.assertEqual(self.evaluate(post), "url")

    def test_media_sensitive_self_and_missing_author_are_skipped(self):
        cases = [
            ({"attachments": {"media_keys": ["3_123"]}}, "media_or_sensitive"),
            ({"possibly_sensitive": True}, "media_or_sensitive"),
            ({"author_id": "10"}, "self"),
            ({"author_id": ""}, "missing_author"),
        ]
        for overrides, reason in cases:
            with self.subTest(overrides=overrides):
                self.assertEqual(self.evaluate(incoming(**overrides)), reason)

    def test_author_profile_is_required_and_filtered(self):
        self.assertEqual(self.evaluate(users={}), "missing_author_profile")
        for author in ({"name": "Spam", "username": "reader"},
                       {"name": "Reader", "username": "scam"}):
            with self.subTest(author=author):
                self.assertEqual(self.evaluate(users={"20": author}), "filtered_author")

    def test_opted_out_author_cannot_reenter_through_new_comment(self):
        blocked = {item_hash("user", "20")}
        self.assertEqual(self.evaluate(opted_out=blocked), "opted_out")
        self.assertEqual(self.evaluate(incoming(id="201", text="@SeoYuna Thank you!"), opted_out=blocked), "opted_out")

    def test_stop_helper_normalizes_only_complete_optout_commands(self):
        for body in ("@SeoYuna STOP!!!", "@seoyuna  stop   replies.", "@SeoYuna ＳＴＯＰ",
                     "@SeoYuna unsubscribe", "@SeoYuna opt-out", "@SeoYuna jangan balas"):
            with self.subTest(body=body):
                self.assertTrue(is_opt_out(incoming(text=body), ACCOUNT))
        for body in ("@SeoYuna Don't stop posting!", "@SeoYuna a bus stop", "@SeoYuna stopgap"):
            with self.subTest(body=body):
                self.assertFalse(is_opt_out(incoming(text=body), ACCOUNT))
        self.assertFalse(is_opt_out(incoming(text="@SeoYuna stop", author_id="10"), ACCOUNT))

    def test_stop_is_recognized_before_normal_eligibility_filters(self):
        """STOP may be an ordinary mention or contain '?' and still needs honoring."""
        stop = incoming(id="201", text="@SeoYuna STOP?", referenced_posts=[], in_reply_to_user_id=None)
        self.assertTrue(is_opt_out(stop, ACCOUNT))
        fetched_batch = [incoming(id="200"), stop]
        opted_out = {item_hash("user", post["author_id"]) for post in fetched_batch
                     if is_opt_out(post, ACCOUNT)}
        self.assertEqual(self.evaluate(fetched_batch[0], opted_out=opted_out), "opted_out")


if __name__ == "__main__":
    unittest.main()
