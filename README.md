# ZOVAK — Python-first AI Receptionist Backend

This repository is an **asynchronous backend foundation**, NOT a finished production
AI call center. It is built from the ZOVAK two-page concept brief (Oct 2026),
with OpenAI GPT-Live **client delegation** as the proposed future voice architecture.

## Current implementation status

| Feature | Status |
| --- | --- |
| FastAPI async health, local OpenAI Agents SDK chat | Implemented; chat needs API key |
| One receptionist agent, tools, multi-turn local memory | Implemented; not evaluated yet |
| Demo services, available slots, booking proposal/confirmation | Implemented (memory only, single process) |
| PostgreSQL schema models, Docker Compose | Draft schema only; no migrations/real storage |
| Google Calendar freeBusy/event creation | Adapter implemented; OAuth + wiring pending |
| Gmail send, Twilio SMS, Meta WhatsApp send | REST adapters implemented; credentials + routing pending |
| GPT-Live WebRTC session factory | Helper implemented; sideband, auth and frontend pending |
| Telephone/SIP, inbound email/chat/WhatsApp | Planned, NOT implemented |
| Website booking card: typed email, Confirm booking button | Implemented for web voice + chat (dev routes) |
| Move / cancel a booking made in the same conversation | Implemented (memory only; Google event moved/deleted) |
| 1-hour appointment reminder email via Resend | Implemented (only while the server runs; set `RESEND_API_KEY`, `REMINDER_FROM_EMAIL`) |
| Phone calls via GPT-Live SIP (Linphone for free testing) + one-time SMS booking form | Implemented; set `OPENAI_WEBHOOK_SECRET`, `PUBLIC_BASE_URL`; SMS goes to a dev outbox |
| Find bookings from earlier conversations, waitlists, recovery, dashboards | Planned, NOT implemented |

## Setup (Python 3.11+)

```bash
cd zovak-backend
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -e '.[dev]'
cp .env.example .env
# Set your own OPENAI_API_KEY in .env; NEVER commit your key
# For local-only development, the .env template already enables dev endpoints.
uvicorn zovak.main:app --reload --host 127.0.0.1 --port 8000
```

You do not need Docker or Google credentials for the first simulated booking.

**Verification:** Syntax compilation, import of SQLAlchemy schema, and seven isolated
local tests passed in the build environment. Real SDK session execution, OAuth,
GPT-Live sideband, and provider calls were **not** tested end-to-end: external
Python package downloads were unavailable in the build environment. Run the
installation and tests on your machine before further development.

```bash
pytest -q
curl http://127.0.0.1:8000/health
```

In another terminal, post messages to `/api/v1/dev/chat` following
[API examples](docs/API_REFERENCE.md). Conversation state persists to `.data/dev_conversations.db`.
The booking proposals themselves do **not** persist across restarts.

**Security:** Only run development routes on your own laptop.
They accept untrusted tenant IDs, have no authentication, and are purposely disabled
outside `APP_ENV=development`. They cannot be promoted directly to production.

## Why services before full FastAPI?

The receptionist calls tool functions that use `DemoScheduler`, not HTTP endpoints.
FastAPI presently provides only a local transport for testing text chat and confirms.
Once the agent tools are evaluated, we'll implement the durable multi-tenant
Postgres and OAuth services and expose secure APIs. That prevents accidental coupling
of tool reasoning to transport infrastructure.

## Voice design

`providers/voice/gpt_live.py` creates the WebRTC session from the browser's SDP
using the official Python SDK. Production requires tenant authentication,
server-side sideband, final-transcript accumulation, deduplicated delegation,
secure backend orchestration, proper permissions and error handling.
Phone numbers must be purchased/ported/forwarded through an eligible provider.
For production SIP, see official OpenAI GPT-Live [Telephony and SIP](https://developers.openai.com/api/docs/guides/voice-sip).

## Documentation used

- [OpenAI Agents SDK Python](https://openai.github.io/openai-agents-python/)
- [OpenAI GPT-Live](https://developers.openai.com/api/docs/guides/live)
- [OpenAI client delegation](https://developers.openai.com/api/docs/guides/live-delegation)
- [Google Calendar free/busy and events](https://developers.google.com/workspace/calendar/api/guides/create-events)
- [Gmail send](https://developers.google.com/workspace/gmail/api/guides/sending)
- [Twilio Voice trial](https://www.twilio.com/docs/usage/tutorials/how-to-use-your-free-trial-account)

Next steps: [Detailed staged plan](docs/IMPLEMENTATION_PLAN.md).
