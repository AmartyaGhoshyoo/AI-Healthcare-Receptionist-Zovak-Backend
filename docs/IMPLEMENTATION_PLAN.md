# ZOVAK implementation plan — gated milestones

This plan preserves the concept brief's product scope but implements it in verified stages.
Each milestone requires security and failure tests before advancement.

## M0 — Agent foundation (included in starter)
- [x] Project structure and async FastAPI health route.
- [x] One OpenAI Agents SDK receptionist, tool contract, local chat route.
- [x] Mock service catalog, availability, proposal, explicit confirmation.
- [x] Google Calendar, Gmail, SMS, WhatsApp async HTTP adapter examples (not connected).
- [x] GPT-Live WebRTC creation helper (not connected; no browser/sideband yet).
- [x] Initial Postgres ORM entity definitions; not deployed and not migrated yet.
- [ ] Manually validate agent conversations using real OpenAI credentials.

## M1 — Core production scheduling
- [ ] Tenant onboarding, sessions/authentication, RBAC and tenant-aware permissions.
- [ ] Migration scripts + async PostgreSQL repositories, organization/service/staff models.
- [ ] Google OAuth 2.0 PKCE, CSRF state, refresh-token rotation, encryption at rest and revoke.
- [ ] Google free/busy, booking, update/delete, Google event IDs and correct calendar permissions.
- [ ] UTC persistence with IANA timezone display, DST, holidays, buffers, staff/resources.
- [ ] Durable booking state machine: pending -> committed or failed, idempotency keys.
- [ ] Database conflict protection, external calendar race mitigation, reconciliation/outbox.
- [ ] Webhook validation, polling fallback, quota/backoff, audit logging.
- [ ] Integration tests against a dedicated Google test calendar; no actual customer data.

## M2 — Communication
- [ ] Gmail OAuth and outbound email queue with delivery failures and opt-out policy.
- [ ] SMS provider, regional restrictions, opt-in/out, rate limits and retry policies.
- [ ] WhatsApp Cloud API inbound validation, allowed window/templates, opt-in, delivery receipts.
- [ ] Shared omnichannel conversation identity with verification before merging users.
- [ ] Confirmation/reminder scheduler with persisted jobs and retry semantics.

## M3 — Voice
- [ ] GPT-Live WebRTC web audio, client delegation and server-side sideband.
- [ ] Store final transcripts; dedupe fragment/replay events; protect privacy and consent.
- [ ] Preserve delegation IDs, cancel/ignore superseded tasks, communicate only verified results.
- [ ] Telephony via routed SIP number, verified incoming webhook, accept call and attach sideband.
- [ ] Transfer/escalate call and test dropped connections; privacy/recording compliance.

## M4 — Operations and scaling
- [ ] Staff absence/mass-recovery planner, impact priorities, customer proposals.
- [ ] Resource coordination, temporary holds, dynamic waitlist, cancellation backfill.
- [ ] Manager approval, customer negotiation, verified writes, notifications and rollback planning.
- [ ] Redis/background worker, dashboards, metrics, error budget and cost monitoring.
- [ ] Load tests, conversation and voice evaluations, failure injection, CI/CD deploy.

## Core safety invariants
1. A customer conversation is not authorization to access another customer's appointments.
2. The LLM cannot select or override a tenant identity; the backend derives it from authentication.
3. The LLM can propose but cannot silently commit appointment operations.
4. Never say "booked" until source calendar/system confirms and the local state reconciles.
5. Do not conflate "voice interruption" with cancellation of underlying work.
6. Provider webhooks must be signature verified and idempotently processed.
7. Sending SMS/WhatsApp/email requires lawful messaging consent and permitted templates/windows.
8. Any customer identity merging across channels needs a verification strategy.
9. AI APIs, phone calls and often outbound messages are not guaranteed to be free.
10. The mock scheduler and unauthenticated development routes must never be production deployed.
