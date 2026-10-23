# Feature-to-milestone mapping (ZOVAK Oct 2026 concept brief)

| Concept-brief requirement | Intended module / capability | Stage |
| --- | --- | --- |
| Phone, website chat, SMS, WhatsApp, email, voice notes | `providers/voice`, `providers/notifications`, future channels | M2–M3 |
| Shared conversation context and cross-channel identity | `conversations` + verified identity-linking | M2 |
| Multilingual receptionist, FAQ, lead intake, routing | `agents/receptionist.py`, tenant KB, human handoff | M0 basic; M2 mature |
| Availability, service durations, staff, location, room, resources | `services/`, `db/models.py`, calendar adapters | M1 |
| Book/reschedule/cancel; Google, Outlook, Calendly | calendar adapter interface + scheduling domain | M1+ |
| Confirm only after verified external transaction | pending->confirmed application state machine, reconciliation | M0 mock; M1 durable |
| Verification, deposits, spam/duplicates and policies | auth, policies, payments adapter | M1–M4 |
| Reminders, summaries, transcripts, notifications | outbox workers, channel adapters, privacy controls | M2–M3 |
| Staff absence, delays, closures and schedule-drift recovery | `workflows/recovery.py`, durable saga engine | M4 |
| Customer-impact optimization and fairness | recovery planner + explainable priority score | M4 |
| Dynamic waitlist and re-filling cancellations | waitlist domain services | M4 |
| Conversational negotiation of alternatives | agent + verified proposals, consent and confirmations | M4 |
| Admin dashboard, logs and analytics | audit schema, API queries, frontend | M4 |

**Note:** A defined module, data model or adapter does not imply that a production
feature is implemented. See `README.md` for the exact current status.
