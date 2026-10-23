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
