"""Optional, explicitly enabled authentication/billing email alerts."""
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import smtplib
import ssl


def maybe_send_alert(env, store, *, status, now=None, smtp_factory=smtplib.SMTP):
    """Caller restricts this to live runs. Returns a non-sensitive outcome label."""
    if env.get("ENABLE_EMAIL_ALERTS", "").strip().lower() != "true":
        return "disabled"
    if status not in {401, 402, 403}:
        return "not_applicable"
    sender = env.get("GMAIL_ADDRESS", "").strip()
    password = env.get("GMAIL_APP_PASSWORD", "").strip()
    recipients = [part.strip() for part in env.get("NOTIFICATION_EMAIL", "").split(",") if part.strip()]
    if not sender or not password or not recipients:
        return "missing_email_secrets"
    if any("\r" in value or "\n" in value or "@" not in value for value in [sender, *recipients]):
        return "invalid_email_configuration"
    now = now or datetime.now(timezone.utc)
    state = store.load()
    if state["last_alert_at"]:
        previous = datetime.fromisoformat(state["last_alert_at"].replace("Z", "+00:00"))
        if now - previous < timedelta(hours=24):
            return "throttled"
    # Commit the cooldown before contacting SMTP, so interruption cannot flood mail.
    state["last_alert_at"] = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    store.save(state)
    message = EmailMessage()
    message["Subject"] = "[Action required] X reply manager stopped"
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(
        f"The X reply manager stopped after API HTTP {status}.\n\n"
        "Check the GitHub Actions run and X Developer Console.\n"
        "401: verify the four OAuth1 credentials and Read and write permission.\n"
        "402: check API credits/billing.\n"
        "403: check write permission, account restrictions, and reply eligibility.\n\n"
        "A blocked reply remains in persistent state until you resolve it.\n"
        "No keys, incoming comment text, or user handles are included in this email.\n"
    )
    with smtp_factory("smtp.gmail.com", 587, timeout=30) as server:
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
        server.login(sender, password)
        server.send_message(message)
    return "sent"
