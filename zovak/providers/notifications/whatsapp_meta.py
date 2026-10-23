"""WhatsApp Cloud API adapter; requires Meta credentials and allowed template/24h window."""
import httpx


async def send_whatsapp_text(access_token: str, phone_number_id: str,
                             recipient: str, text: str, *, graph_api_version: str = "v23.0",
                             client: httpx.AsyncClient | None = None) -> str:
    # Verify Graph API version at time of deployment; Meta deprecates versions.
    endpoint = f"https://graph.facebook.com/{graph_api_version}/{phone_number_id}/messages"
    payload = {"messaging_product": "whatsapp", "to": recipient, "type": "text",
               "text": {"body": text}}
    headers = {"Authorization": f"Bearer {access_token}"}
    if client is None:
        async with httpx.AsyncClient(timeout=15) as http:
            response = await http.post(endpoint, json=payload, headers=headers)
    else:
        response = await client.post(endpoint, json=payload, headers=headers)
    response.raise_for_status()
    return str(response.json()["messages"][0]["id"])
