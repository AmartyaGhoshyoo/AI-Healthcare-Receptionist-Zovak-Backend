"""Future voice entry point: GPT-Live WebRTC session creation.

Not exposed as an HTTP endpoint until authentication, rate limiting and sideband
handling are complete. The browser generates SDP; Python can create the session.
"""
from openai import AsyncOpenAI

LIVE_PROMPT = (
    "You are ZOVAK's voice receptionist. Speak naturally and briefly. "
    "Ask for clarification when necessary. Delegate requests involving scheduling, "
    "customer records, messages or business policy to the backend. "
    "Do not claim actions completed without verified backend results."
)


async def create_live_session(sdp_offer: str) -> tuple[str, str]:
    async with AsyncOpenAI() as client:
        live = await client.live.create(
            session={
                "model": "gpt-live-1",
                "instructions": LIVE_PROMPT,
                "delegation": {"type": "client"},
            },
            transport={"type": "webrtc", "sdp": sdp_offer},
        )
    return live.session.id, live.transport.sdp


# ZOVAK_VOICE_CONFIRMATION_PROMPT_V1
# Add to the existing voice prompt WITHOUT changing the session creation API.
LIVE_PROMPT += (
    " After a pending appointment proposal, the backend will ask the "
    "customer politely whether they are sure. Always delegate the customer's "
    "next reply to the backend, even if the reply is short, such as yes, no, "
    "please confirm it, or change the time. Never claim a booking is confirmed "
    "unless the backend returns verified confirmation."
)
