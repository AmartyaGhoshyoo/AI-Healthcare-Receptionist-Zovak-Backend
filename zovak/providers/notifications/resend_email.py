"""Async Resend email sending adapter. Consent and scheduling live above this layer."""
import httpx


async def send_resend_email(api_key: str, sender: str, recipient: str, subject: str,
                            html: str, text: str, *, idempotency_key: str,
                            client: httpx.AsyncClient | None = None) -> str:
    """Send one email. The idempotency key makes a retried send deliver only once."""
    request = {
        "url": "https://api.resend.com/emails",
        "headers": {"Authorization": f"Bearer {api_key}", "Idempotency-Key": idempotency_key},
        "json": {"from": sender, "to": [recipient], "subject": subject, "html": html, "text": text},
    }
    if client is None:
        async with httpx.AsyncClient(timeout=15) as http:
            response = await http.post(**request)
    else:
        response = await client.post(**request)
    response.raise_for_status()
    message_id = response.json().get("id")
    if not message_id:
        raise RuntimeError("Resend did not return a message ID")
    return str(message_id)
