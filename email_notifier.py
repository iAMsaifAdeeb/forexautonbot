"""Email alerts via Resend API (domain: usdtlocal.com)."""

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger("bot.email")

RESEND_URL = "https://api.resend.com/emails"


def send_email(config: dict, subject: str, body: str) -> bool:
    if not config.get("email_enabled", True):
        log.info("Email skipped (disabled): %s", subject)
        return False

    api_key = (config.get("resend_api_key") or "").strip()
    if not api_key:
        log.warning("Email not sent — set resend_api_key in Settings (⚙ → Email alerts).")
        return False

    to_addr = (config.get("email_to") or "saifadeeb@gmail.com").strip()
    from_addr = (config.get("email_from") or "Gold Sniper <bot@usdtlocal.com>").strip()

    payload = json.dumps({
        "from": from_addr,
        "to": [to_addr],
        "subject": subject,
        "text": body,
    }).encode("utf-8")

    request = urllib.request.Request(
        RESEND_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode())
        log.info("Email sent via Resend: %s → %s (id %s)",
                 subject, to_addr, result.get("id", "?"))
        return True
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        log.error("Resend HTTP %s (%s): %s", exc.code, subject, detail)
        return False
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
