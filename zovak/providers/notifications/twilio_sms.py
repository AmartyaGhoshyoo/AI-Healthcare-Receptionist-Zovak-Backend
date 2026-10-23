"""Async SMS provider example. Trial or paid Twilio credentials required."""
import httpx


async def send_sms(account_sid: str, auth_token: str, sender: str,
                   recipient: str, body: str, *, client: httpx.AsyncClient | None = None) -> str:
    endpoint = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    payload = {"From": sender, "To": recipient, "Body": body}
    if client is None:
        async with httpx.AsyncClient(timeout=15) as http:
            response = await http.post(endpoint, data=payload, auth=(account_sid, auth_token))
    else:
        response = await client.post(endpoint, data=payload, auth=(account_sid, auth_token))
    response.raise_for_status()
    return str(response.json()["sid"])
