# ZOVAK service boundaries

```text
Web chat / WhatsApp / SMS / Email / WebRTC / PSTN (SIP)
                    |
           authenticated channel adapters
                    |
          identity + consent + tenant routing
                    |
         shared Python receptionist agent
          |             |              |
    tools: read       proposals      escalation
          |             |              |
       Scheduling / Rules / Booking domain services
          |       |        |          |
       Postgres   Calendar   Outbox    Human operators
                   adapter   worker
                   |           |
                 Google    Gmail / Meta / SMS
                 Outlook
```

GPT-Live is **only** the voice conversation front-end; with client delegation,
our server assembles final transcripts and safe per-tenant context, runs the
same backend reasoning tools and returns only verified results for speech.

## Why an adapter interface?
The scheduling domain asks a calendar adapter whether a slot is free and then
creates an event. It should not depend on Google/Outlook SDK-specific types.
Cross-calendar locking is a separate application responsibility; provider
free/busy + event insert are **not** atomic together.

## Agent tooling policy
- Read-only: list services, check availability, view own bookings and FAQs.
- Proposed action: generate a pending proposal with expiry; no mutation.
- Mutating action: requires verified actor, authorization, scoped confirmation,
  idempotency key, fresh conflict checks and durable provider state.
- High-impact actions: staff absence/mass-rescheduling must be approved by a
  manager with authorization; customer acceptance may be required separately.

## Production integrations
- Google Calendar: organization-authorized OAuth; calendar scopes as limited as
  needed, credentials encrypted and rotated; actual availability and event calls.
- Gmail: authorized Gmail sender, scopes, consent, unsubscribe handling.
- SMS: provider number, E.164 numbers, compliance, opt-in/out, failure reports.
- WhatsApp: organization phone number, business verification, template/window
  rules, delivery webhooks, customer opt-in and business-initiated messaging charges.
- Telephony: a purchasable/ported/forwarded number, SIP trunk to GPT-Live or
  provider media stream, verified incoming webhook and sideband delegation.

## Security tasks required before production
- Replace local tenant string with verified identity and server-derived tenant ID.
- Split API keys, OAuth tokens, and encryption keys into managed secret store.
- Install Alembic migrations; enforce PostgreSQL role permissions and DB constraints.
- Add PII data-retention, access-control and transcript privacy policies.
- Cross-worker idempotency/locking and calendar reconciliation.
- Deduplicate messages and callbacks from webhook retries and cross-channel requests.
- Contract tests with supported providers and service-specific failure scenarios.
