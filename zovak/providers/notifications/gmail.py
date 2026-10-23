"""Async Gmail REST sending adapter. OAuth and consent live above this layer."""
import base64
from email.message import EmailMessage
import httpx


async def send_gmail(access_token: str, sender: str, recipient: str,
                     subject: str, body: str, *, client: httpx.AsyncClient | None = None) -> str:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    if client is None:
        async with httpx.AsyncClient(timeout=15) as http:
            response = await http.post(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                headers={"Authorization": f"Bearer {access_token}"}, json={"raw": encoded},
            )
    else:
        response = await client.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={"Authorization": f"Bearer {access_token}"}, json={"raw": encoded},
        )
    response.raise_for_status()
    return str(response.json()["id"])
