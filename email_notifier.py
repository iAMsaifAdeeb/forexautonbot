"""Email alerts for target reached, new-day duty, and startup test."""

import logging
import smtplib
import ssl
from email.mime.text import MIMEText

log = logging.getLogger("bot.email")


def send_email(config: dict, subject: str, body: str) -> bool:
    if not config.get("email_enabled", True):
        log.info("Email skipped (disabled): %s", subject)
        return False

    to_addr = config.get("email_to", "saifadeeb@gmail.com")
    user = config.get("smtp_user") or ""
    password = config.get("smtp_password") or ""
    if not user or not password:
        log.warning("Email not sent — set smtp_user and smtp_password in Settings.")
        return False

    host = config.get("smtp_server", "smtp.gmail.com")
    port = int(config.get("smtp_port", 587))
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr

    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.login(user, password)
            server.sendmail(user, [to_addr], msg.as_string())
        log.info("Email sent: %s → %s", subject, to_addr)
        return True
    except Exception as exc:
        log.error("Email failed (%s): %s", subject, exc)
        return False


def notify_target_completed(config: dict, equity: float, target_pct: float) -> bool:
    return send_email(
        config,
        "Gold Sniper — Target Completed",
        f"Daily target completed.\n\n"
        f"Equity: {equity:,.2f}\n"
        f"Target: +{target_pct}%\n\n"
        f"Bot stopped trading for today.",
    )


def notify_on_duty(config: dict) -> bool:
    return send_email(
        config,
        "Gold Sniper — On Duty",
        "I am On My Duty :D Start working.",
    )


def notify_test_flight(config: dict) -> bool:
    return send_email(
        config,
        "Gold Sniper — Test Flight",
        "Test flight Completed.",
    )
