"""Offline checks for CLI side effects and optional, bounded email alerts."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
import unittest
from unittest import mock

import x_auto_engage as cli
from xmanage.alerts import maybe_send_alert
from xmanage.errors import ApiError
from xmanage.state import StateError, initial_state, validate_state


NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
FAKE_X_ENV = {
    "X_EXPECTED_USERNAME": "SeoYuna",
    "X_API_KEY": "fake-key", "X_API_KEY_SECRET": "fake-key-secret",
    "X_ACCESS_TOKEN": "fake-token", "X_ACCESS_TOKEN_SECRET": "fake-token-secret",
}
FAKE_EMAIL_ENV = {
    "ENABLE_EMAIL_ALERTS": "true",
    "GMAIL_ADDRESS": "sender@example.test",
    "GMAIL_APP_PASSWORD": "FAKE_PRIVATE_APP_PASSWORD",
    "NOTIFICATION_EMAIL": "first@example.test, second@example.test",
}


class MemoryAlertStore:
    def __init__(self, state=None, *, events=None, fail_save=False):
        self.remote = deepcopy(initial_state() if state is None else state)
        self.events = events if events is not None else []
        self.fail_save = fail_save
        self.loads = 0
        self.saves = 0

    def load(self):
        self.loads += 1
        self.events.append("load")
        return deepcopy(self.remote)

    def save(self, state):
        validate_state(state)
        self.saves += 1
        self.events.append("save")
        if self.fail_save:
            raise StateError("Injected unconfirmed cooldown write.")
        self.remote = deepcopy(state)


class FakeSMTP:
    def __init__(self, events):
        self.events = events
        self.login_args = None
        self.messages = []
        self.tls_context = None

    def __enter__(self):
        self.events.append("smtp_enter")
        return self

    def __exit__(self, *_):
        self.events.append("smtp_exit")

    def ehlo(self):
        self.events.append("ehlo")

    def starttls(self, *, context):
        self.events.append("starttls")
        self.tls_context = context

    def login(self, sender, password):
        self.events.append("login")
        self.login_args = (sender, password)

    def send_message(self, message):
        self.events.append("send_message")
        self.messages.append(deepcopy(message))


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        # A missed mock must fail instead of opening either X/GitHub or SMTP.
        network = mock.patch("socket.create_connection", side_effect=AssertionError("No network in offline tests"))
        self.network = network.start()
        self.addCleanup(network.stop)
        http = mock.patch("requests.sessions.Session.request", side_effect=AssertionError("No HTTP in offline tests"))
        self.http = http.start()
        self.addCleanup(http.stop)
        environment = mock.patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)


class CliSideEffectTests(OfflineTestCase):
    def test_validate_has_no_x_smtp_or_state_access(self):
        with mock.patch.object(cli, "XClient") as client, mock.patch.object(cli, "make_store") as store, \
                mock.patch.object(cli, "maybe_send_alert") as alert, mock.patch.object(cli, "report") as report:
            self.assertEqual(cli.main(["validate"]), 0)
        client.assert_not_called()
        store.assert_not_called()
        alert.assert_not_called()
        self.http.assert_not_called()
        self.network.assert_not_called()
        self.assertEqual(report.call_args.args[0]["network_requests"], 0)

    def test_disabled_live_mode_rejects_before_client_creation(self):
        with mock.patch.dict(os.environ, FAKE_X_ENV), mock.patch.object(cli, "XClient") as client, \
                mock.patch.object(cli, "make_store") as store, mock.patch.object(cli, "maybe_send_alert") as alert:
            with self.assertLogs(level="ERROR"):
                self.assertEqual(cli.main(["run", "--mode", "live"]), 1)
        client.assert_not_called()
        store.assert_not_called()
        alert.assert_not_called()
        self.http.assert_not_called()
        self.network.assert_not_called()

    def test_missing_credentials_never_construct_client_or_make_http_requests(self):
        with mock.patch.dict(os.environ, {"X_EXPECTED_USERNAME": "SeoYuna"}), \
                mock.patch.object(cli, "XClient") as client, mock.patch.object(cli, "make_store") as store, \
                mock.patch.object(cli, "maybe_send_alert") as alert:
            with self.assertLogs(level="ERROR"):
                self.assertEqual(cli.main(["check"]), 1)
        client.assert_not_called()
        store.assert_not_called()
        alert.assert_not_called()
        self.http.assert_not_called()
        self.network.assert_not_called()

    def test_failed_check_never_emails_even_when_alerts_enabled(self):
        env = {**FAKE_X_ENV, **FAKE_EMAIL_ENV, "X_AUTOMATION_ENABLED": "true"}
        with mock.patch.dict(os.environ, env), mock.patch.object(cli, "XClient") as constructor, \
                mock.patch.object(cli, "make_store") as store, mock.patch.object(cli, "maybe_send_alert") as alert:
            constructor.return_value.me.side_effect = ApiError("Authentication failed", status=401)
            with self.assertLogs(level="ERROR"):
                self.assertEqual(cli.main(["check"]), 1)
        alert.assert_not_called()
        store.assert_not_called()
        constructor.return_value.close.assert_called_once()
        self.network.assert_not_called()

    def test_default_and_explicit_dry_run_failures_never_email(self):
        env = {**FAKE_X_ENV, **FAKE_EMAIL_ENV, "X_AUTOMATION_ENABLED": "true"}
        for arguments in (["run"], ["run", "--mode", "dry-run"]):
            with self.subTest(arguments=arguments), mock.patch.dict(os.environ, env), \
                    mock.patch.object(cli, "XClient") as constructor, mock.patch.object(cli, "make_store"), \
                    mock.patch.object(cli, "run_manager", side_effect=ApiError("Billing", status=402)) as run, \
                    mock.patch.object(cli, "maybe_send_alert") as alert:
                with self.assertLogs(level="ERROR"):
                    self.assertEqual(cli.main(arguments), 1)
                self.assertEqual(run.call_args.kwargs["mode"], "dry-run")
                alert.assert_not_called()
                constructor.return_value.close.assert_called_once()
        self.network.assert_not_called()

    def test_status_reads_state_without_x_client_or_local_content_setup(self):
        store = MemoryAlertStore()
        with mock.patch.object(cli, "make_store", return_value=store), mock.patch.object(cli, "XClient") as client, \
                mock.patch.object(cli, "setup_content") as setup, mock.patch.object(cli, "maybe_send_alert") as alert, \
                mock.patch.object(cli, "report") as report:
            self.assertEqual(cli.main(["status"]), 0)
        client.assert_not_called()
        setup.assert_not_called()
        alert.assert_not_called()
        self.assertEqual((store.loads, store.saves), (1, 0))
        self.assertFalse(report.call_args.args[0]["initialized"])

    def test_cli_does_not_treat_429_as_email_alert_failure(self):
        env = {**FAKE_X_ENV, **FAKE_EMAIL_ENV, "X_AUTOMATION_ENABLED": "true"}
        with mock.patch.dict(os.environ, env), mock.patch.object(cli, "XClient"), \
                mock.patch.object(cli, "make_store"), mock.patch.object(cli, "maybe_send_alert") as alert, \
                mock.patch.object(cli, "run_manager", side_effect=ApiError("Rate limited", status=429)):
            with self.assertLogs(level="ERROR"):
                self.assertEqual(cli.main(["run", "--mode", "live"]), 1)
        alert.assert_not_called()

    def test_optional_alert_error_cannot_leak_smtp_exception_details(self):
        marker = "FAKE_PASSWORD_MUST_NOT_APPEAR_IN_LOG"
        env = {**FAKE_X_ENV, **FAKE_EMAIL_ENV, "X_AUTOMATION_ENABLED": "true"}
        with mock.patch.dict(os.environ, env), mock.patch.object(cli, "XClient"), \
                mock.patch.object(cli, "make_store"), \
                mock.patch.object(cli, "run_manager", side_effect=ApiError("Authentication failed", status=401)), \
                mock.patch.object(cli, "maybe_send_alert", side_effect=RuntimeError(marker)) as alert:
            with self.assertLogs(level="WARNING") as logs:
                self.assertEqual(cli.main(["run", "--mode", "live"]), 1)
        alert.assert_called_once()
        self.assertNotIn(marker, "\n".join(logs.output))
        self.assertIn("RuntimeError", "\n".join(logs.output))


class AlertTests(OfflineTestCase):
    def test_disabled_or_missing_secrets_never_reads_or_writes_state_or_smtp(self):
        cases = [({}, "disabled"), ({**FAKE_EMAIL_ENV, "ENABLE_EMAIL_ALERTS": "false"}, "disabled")]
        for key in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "NOTIFICATION_EMAIL"):
            env = dict(FAKE_EMAIL_ENV)
            del env[key]
            cases.append((env, "missing_email_secrets"))
        for env, expected in cases:
            with self.subTest(expected=expected, present=sorted(env)):
                store, smtp = MemoryAlertStore(), mock.Mock()
                self.assertEqual(maybe_send_alert(env, store, status=401, now=NOW, smtp_factory=smtp), expected)
                self.assertEqual((store.loads, store.saves), (0, 0))
                smtp.assert_not_called()
        self.network.assert_not_called()

    def test_429_and_unrelated_statuses_never_send_alerts(self):
        for status in (200, 400, 404, 429, 500, None):
            with self.subTest(status=status):
                store, smtp = MemoryAlertStore(), mock.Mock()
                self.assertEqual(maybe_send_alert(FAKE_EMAIL_ENV, store, status=status, now=NOW,
                                                 smtp_factory=smtp), "not_applicable")
                self.assertEqual((store.loads, store.saves), (0, 0))
                smtp.assert_not_called()

    def test_invalid_recipient_configuration_cannot_write_or_open_smtp(self):
        for key, value in (("GMAIL_ADDRESS", "sender"),
                           ("NOTIFICATION_EMAIL", "user@example.test\nBcc: outsider@example.test"),
                           ("NOTIFICATION_EMAIL", "missing-at-sign")):
            with self.subTest(key=key):
                store, smtp = MemoryAlertStore(), mock.Mock()
                env = {**FAKE_EMAIL_ENV, key: value}
                self.assertEqual(maybe_send_alert(env, store, status=401, now=NOW,
                                                 smtp_factory=smtp), "invalid_email_configuration")
                self.assertEqual((store.loads, store.saves), (0, 0))
                smtp.assert_not_called()

    def test_auth_and_billing_alerts_share_a_24_hour_cooldown(self):
        state = initial_state()
        state["last_alert_at"] = (NOW - timedelta(hours=24) + timedelta(seconds=1)).isoformat()
        store, smtp = MemoryAlertStore(state), mock.Mock()
        for status in (401, 402, 403):
            with self.subTest(status=status):
                self.assertEqual(maybe_send_alert(FAKE_EMAIL_ENV, store, status=status, now=NOW,
                                                 smtp_factory=smtp), "throttled")
        self.assertEqual(store.saves, 0)
        smtp.assert_not_called()
        store.remote["last_alert_at"] = (NOW - timedelta(hours=24)).isoformat()
        server = FakeSMTP([])
        smtp.return_value = server
        self.assertEqual(maybe_send_alert(FAKE_EMAIL_ENV, store, status=402, now=NOW,
                                         smtp_factory=smtp), "sent")
        self.assertEqual(len(server.messages), 1)
        self.assertEqual(store.saves, 1)

    def test_cooldown_is_durable_before_smtp_and_tls_precedes_credentials(self):
        events = []
        store = MemoryAlertStore(events=events)
        server = FakeSMTP(events)
        def smtp_factory(host, port, timeout):
            self.assertEqual((host, port, timeout), ("smtp.gmail.com", 587, 30))
            self.assertEqual(store.remote["last_alert_at"], NOW.isoformat(timespec="seconds"))
            events.append("smtp_open")
            return server
        self.assertEqual(maybe_send_alert(FAKE_EMAIL_ENV, store, status=401, now=NOW,
                                         smtp_factory=smtp_factory), "sent")
        self.assertLess(events.index("save"), events.index("smtp_open"))
        self.assertLess(events.index("starttls"), events.index("login"))
        self.assertLess(events.index("login"), events.index("send_message"))
        self.assertEqual(server.login_args, (FAKE_EMAIL_ENV["GMAIL_ADDRESS"], FAKE_EMAIL_ENV["GMAIL_APP_PASSWORD"]))

    def test_unconfirmed_cooldown_write_prevents_smtp(self):
        store, smtp = MemoryAlertStore(fail_save=True), mock.Mock()
        with self.assertRaises(StateError):
            maybe_send_alert(FAKE_EMAIL_ENV, store, status=401, now=NOW, smtp_factory=smtp)
        smtp.assert_not_called()
        self.assertIsNone(store.remote["last_alert_at"])

    def test_smtp_failure_keeps_cooldown_to_prevent_repeated_delivery_attempts(self):
        store = MemoryAlertStore()
        smtp = mock.Mock(side_effect=OSError("fake SMTP interruption"))
        with self.assertRaises(OSError):
            maybe_send_alert(FAKE_EMAIL_ENV, store, status=403, now=NOW, smtp_factory=smtp)
        self.assertEqual(store.remote["last_alert_at"], NOW.isoformat(timespec="seconds"))
        self.assertEqual(maybe_send_alert(FAKE_EMAIL_ENV, store, status=403, now=NOW + timedelta(minutes=1),
                                         smtp_factory=smtp), "throttled")
        self.assertEqual(smtp.call_count, 1)

    def test_message_uses_only_configured_recipients_and_generic_diagnostic_content(self):
        private_values = ["FAKE_PRIVATE_X_KEY", "FAKE_PRIVATE_X_TOKEN", "FAKE_PRIVATE_INCOMING_COMMENT",
                          "FAKE_PRIVATE_HANDLE", "FAKE_PRIVATE_RAW_HTTP_BODY", "FAKE_PRIVATE_GITHUB_TOKEN"]
        env = dict(FAKE_EMAIL_ENV)
        env.update(dict(zip(("X_API_KEY", "X_ACCESS_TOKEN", "COMMENT_TEXT", "X_EXPECTED_USERNAME",
                             "RAW_API_BODY", "GITHUB_TOKEN"), private_values)))
        server = FakeSMTP([])
        self.assertEqual(maybe_send_alert(env, MemoryAlertStore(), status=402, now=NOW,
                                         smtp_factory=mock.Mock(return_value=server)), "sent")
        self.assertEqual(len(server.messages), 1)
        message = server.messages[0]
        self.assertEqual(message["From"], "sender@example.test")
        self.assertEqual([address.addr_spec for address in message["To"].addresses],
                         ["first@example.test", "second@example.test"])
        self.assertIsNone(message["Cc"])
        self.assertIsNone(message["Bcc"])
        rendered = message.as_string()
        for value in [*private_values, env["GMAIL_APP_PASSWORD"]]:
            self.assertNotIn(value, rendered)
        self.assertIn("API HTTP 402", message.get_content())
        self.assertEqual(message["Subject"], "[Action required] X reply manager stopped")


if __name__ == "__main__":
    unittest.main()
