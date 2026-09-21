# Autofiera business intelligence

This feature adds an end-to-end, evidence-backed decision-support layer to the existing application. Open `/intelligence/` as an owner or general manager. It reads the same SQLite data as the ERP and does not send messages, buy stock, change prices or approve work.

## What is implemented

- A 7–90 day business snapshot, adjacent-period comparison, completed job value/count/average and service mix.
- Open estimate value and estimates older than seven days, lead cohort won rate, unassigned work, rebooking candidates, low stock, and indicative coverage from logged withdrawals.
- Explicit definitions, source records (first 100 per fact), UTC date boundaries, data-quality alerts and data limitations.
- Natural-language questions and business briefings using the configured Ollama or llama.cpp text provider. The model selects facts and recommendations; displayed claims, money values, record evidence and links come from the server.
- Saved answers preserve the complete snapshot used at answer time, model, mode, duration and question. New data does not rewrite previous answers.
- A business-priority agent that publishes deduplicated findings to the existing Command and Agents screens.
- Database triggers for jobs, estimates, bookings, stock items/movements, substantive lead updates, inspections and customers. Changes enqueue analysis in the same transaction. Repeated events coalesce per studio/event.
- Worker claims and agent leases, retries with backoff, restart recovery, pending/failure status, and owner-authorized retry after five failed attempts.
- Owner/manager access checks, studio-scoped reports, CSRF protection on the new write endpoints, output escaping, bounded question/period inputs and no model-authored SQL or tools.
- Finding lifecycle fixes: recurrence reopens a resolved finding; a severity reduction does not reopen a dismissed finding; evidence JSON is preserved rather than truncated.

## How AI works

`ERP records → tenant-scoped read snapshot → verified facts and recommendations → language model selects relevant IDs → validated selection → server-rendered claims and evidence → saved report`

Financial calculations and operational rules do not depend on a model. The model interprets the question and selects/prioritizes evidence. It cannot fabricate a new displayed amount, database query, action link, or instruction for a write operation. Invalid outputs and provider failures fall back to matching deterministic facts with an explicit mode label. Unsupported questions return a data-limit explanation. This is deliberately constrained question answering, not unrestricted generated business advice.

The primary text model is used for this feature. During local validation `llama3.1:8b` followed the evidence-selection contract more reliably than `llama3.2:3b`; the smaller model can still serve the existing agents' fast tier. Model quality should be evaluated again if changing providers or models.

## Configuration and operation

Use environment variables, not committed credentials:

```sh
AI_ENABLED=1
LLM_BACKEND=ollama
OLLAMA_URL=http://127.0.0.1:11434
OLLAMA_MODEL=llama3.1:8b
OLLAMA_FAST_MODEL=llama3.2:3b
AGENT_SCHEDULER=1
```

The existing application startup runs the idempotent migration. The in-app scheduler drains queued changes and runs periodic agents approximately every minute after a 15-second startup delay. A sweep can take longer while model calls are running. To process events in a separate process against an already initialized database:

```sh
PYTHON_DOTENV_DISABLED=1 DATABASE_URL=/absolute/path/app.sqlite3 python -m src.intelligence_worker --once
```

Omit `--once` for a worker that checks every five seconds. The separate worker processes domain events; periodic time-based checks remain the in-app scheduler's responsibility. Both paths share database leases. Leases expire after 30 minutes; a process that exceeds that duration could overlap a recovery run. This is a SQLite outbox, not an exactly-once distributed queue. Use a managed queue/worker service when moving to distributed deployments.

Five consecutive failures suspend that event group until a new event arrives or an owner selects **Retry failed analysis**. Disabling an agent skips it during scheduled/event work; manual runs remain possible. Check `/intelligence/api/health` and the Agents console for outcomes. No automatic customer communication was added.

## API

- `GET /intelligence/`: dashboard and session CSRF token.
- `GET /intelligence/api/snapshot?days=30`: current facts, definitions, evidence and actions.
- `POST /intelligence/api/ask`: JSON `{ "question": "Which stock needs attention?", "days": 30 }`; requires `X-CSRF-Token` from the authenticated page.
- `GET /intelligence/api/reports/<id>`: immutable saved answer, scoped to the current studio.
- `GET /intelligence/api/health`: pending events and recent runs.
- `POST /intelligence/api/retry-failed`: retry exhausted event groups for the current studio; requires the same CSRF token.

Command and agent feeds now require owner/manager access because they contain business intelligence. The rest of the legacy application has not received a comprehensive authorization/CSRF overhaul.

## Business definitions and limits

- Existing job prices are whole USD; estimates are integer cents. Conversion occurs once at the reporting boundary. Multi-currency accounting is not implemented.
- Completed job value is not cash collection or profit. Missing completion dates are surfaced separately.
- Lead conversion uses current won status of leads created in the selected window; `booking_form` backfilled rows are excluded. This is a maturing cohort, not a predicted closing probability.
- Rebooking uses verified customer links, a fixed 90-day rule and absence of a future non-cancelled booking. It is not a service-specific promise of when a customer is due.
- Stock coverage divides current quantity by withdrawals over 30 days and needs three distinct consumption dates. It assumes complete logs and unchanged demand. It does not account for supplier lead time.
- Exact vehicle condition comparison needs stable vehicle IDs across jobs and validated inspection evidence. Existing DVI/vision functions remain separate; this change does not establish their accuracy.
- Actual bay utilization, technician speed, margin and cash forecasts require working-time, cost and payment records the current schema does not reliably supply. The assistant should not invent those measures.
- Read snapshots and stored report payloads can grow with shop data. Source displays are capped at 100 records per fact, but calculation currently reads matching records into memory. Production scale needs representative load tests, retention policy and aggregate queries.
- Existing repository audit issues (legacy public routes, committed secrets in history, incomplete payment/material workflows and deployment hardening) still need resolution before commercial release.

## Validation

```sh
AI_ENABLED=0 AGENT_SCHEDULER=0 FLASK_DEBUG=0 PYTHON_DOTENV_DISABLED=1 python -m unittest discover tests -v
```

Tests cover money units and period boundaries, tenant-safe joins, incomplete data, stock sample thresholds, PII omission from model context, invalid model replies, evidence attachment, immutable reports, access/CSRF/input validation, transactional queue behavior, retry/recovery, disabled agents, concurrency leases and finding lifecycle regressions. Live model and browser checks use an isolated local demo database.
