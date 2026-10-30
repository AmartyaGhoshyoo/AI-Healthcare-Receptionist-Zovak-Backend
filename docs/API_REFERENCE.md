# Initial local API surface

- GET `/health`: health check. Does not assert external dependencies are healthy.
- POST `/api/v1/dev/chat`: an OpenAI Agents SDK text turn with local persisted chat memory.
- POST `/api/v1/dev/confirm`: explicit confirmation of one pending simulated proposal.

The latter two are intentionally **unauthenticated** and appear only when both
`APP_ENV=development` and `ENABLE_DEV_ROUTES=true`. Bind Uvicorn to `127.0.0.1`.

Example:

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/api/v1/dev/chat \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"demo-business", "conversation_id":"test-1", "message":"What services are available?"}'
```

After receiving a real `proposal_id` from the receptionist:

```bash
curl -s http://127.0.0.1:8000/api/v1/dev/confirm \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"demo-business", "conversation_id":"test-1", "proposal_id":"REPLACE_WITH_ID"}'
```

Responses say `provider=demo-only`. Nothing is written to Google Calendar yet.

## Booking card (website)

Text chat (unauthenticated dev routes, keyed by `conversation_id`):

- GET `/api/v1/dev/booking-state?conversation_id=…`: `stage` is one of `idle`,
  `email_required`, `ready_to_confirm`, `booked`, `move_pending`, `cancel_pending`, `cancelled`.
- POST `/api/v1/dev/proposal-email` / `/api/v1/dev/proposal-email/clear`: set or clear the draft's email. Never books.
- POST `/api/v1/dev/confirm`: the Confirm booking button.
- POST `/api/v1/dev/change/confirm` / `/api/v1/dev/change/discard`: confirm or keep a pending move/cancel.

Web voice (bearer `ui_token` from `POST /api/v1/dev/voice/session`):

- GET `/api/v1/dev/voice/booking-state?conversation_id=…`
- POST `/api/v1/dev/voice/email-input`, `/reopen-email`, `/confirm-booking`, `/change/confirm`, `/change/discard`

The agent can only *propose* bookings, moves (`propose_move`) and cancellations
(`propose_cancellation`). Each needs an explicit tap, or a clear spoken yes on a call.
Only bookings made in the same conversation can be moved or cancelled (in-memory storage).

## Reminders

Set `RESEND_API_KEY` and `REMINDER_FROM_EMAIL` (a sender on a Resend-verified domain) in
`.env`. A background task emails each customer `REMINDER_MINUTES_BEFORE` (default 60)
minutes before their appointment, once. It skips bookings made inside that window and
cancelled bookings, and follows moved ones. Reminders only run while the server is up.
