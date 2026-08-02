# noname-app
# Autofiera — Detailing Studio SaaS Platform

Multi-tenant Flask app for US detailing studios. Each studio logs in with
its own credentials, sees its own logo, jobs, bookings, customers, and data
— fully isolated from every other studio on the platform.

Branch: `feature/vk-001-refactoringtofollowsolidprinciples`

---

## Stack

- **Backend**: Flask (app factory pattern, Blueprint-based routing)
- **Database**: SQLite for now (`studios.db`) — migration path to PostgreSQL planned
- **Auth**: Custom session-based auth (`src/auth.py`) — pbkdf2:sha256 password hashing
- **AI Assistant**: Ollama (local, primary) + Gemini (research/vision fallback) + OpenAI (optional)
- **Frontend**: Jinja2 templates, vanilla CSS, dark theme with gold accent

---

## Project structure

```
noname-app/
├── run.py                      Entry point — run this, not app.py
├── requirements.txt
├── .env                        Local secrets (gitignored, never committed)
├── studios.db                  SQLite DB (gitignored)
├── src/
│   ├── __init__.py             Exposes create_app()
│   ├── app.py                  Flask app factory
│   ├── config.py               All config — DB path, AI keys, secret key
│   ├── controller.py           All routes (Blueprint "main")
│   ├── db_manager.py           DB connection helpers (get_db, close_db, init_db)
│   ├── auth.py                 Password hashing, roles, decorators, permissions
│   ├── ai_service.py           AI router — Ollama / Gemini / OpenAI dispatch
│   ├── reminder_service.py     Follow-up reminder scheduling logic
│   ├── seed.py                 Initial demo data (studios, staff, services, jobs)
│   ├── migrate_auth.py         Adds customers table, hashes legacy plain-text passwords
│   ├── migrate_roles.py        Normalizes staff role values to the 6-role system
│   ├── migrate_media.py        Adds media table (before/during/after photos)
│   ├── migrate_crm.py          Adds vehicles table, backfills customers from bookings
│   ├── migrate_notes.py        Adds notes table (internal vs client-visible)
│   └── migrate_reminders.py    Adds reminders table
├── templates/                  All Jinja2 HTML templates
└── static/
    ├── logos/                  Studio logo files (named per studio in DB)
    └── uploads/                Job media, organized as studio_<id>/
```

---

## First-time setup

```bash
# 1. Clone and switch to this branch
git clone https://github.com/ruthwiik8reddy/noname-app.git
cd noname-app
git checkout feature/vk-001-refactoringtofollowsolidprinciples

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create your local .env (never committed — see .gitignore)
touch .env
```

Add to `.env`:
```
SECRET_KEY=your-own-random-string
GEMINI_API_KEY=your-gemini-key        # optional — get from https://aistudio.google.com/app/apikey
OLLAMA_URL=http://127.0.0.1:11434
OLLAMA_MODEL=llama3.2:3b              # or whatever you have via `ollama list`
```

```bash
# 4. Seed the base data (studios, staff, services, jobs, bays)
python src/seed.py

# 5. Run every migration IN THIS ORDER (each is safe to re-run / idempotent)
python src/migrate_auth.py        # customers table + password hashing
python src/migrate_roles.py       # normalize staff roles to the 6-role system
python src/migrate_media.py       # media table
python src/migrate_crm.py         # vehicles table + customer backfill from bookings
python src/migrate_notes.py       # notes table (internal vs client)
python src/migrate_reminders.py   # reminders table

# 6. (Optional) If you want local AI answers, start Ollama in another terminal
ollama serve
ollama list      # confirm you have a model, e.g. llama3.2:3b

# 7. Run the app
python run.py
```

Visit **http://localhost:5055**

---

## Demo logins (from seed.py)

| Studio | Username | Password | City |
|---|---|---|---|
| Shine Pro Detailing | `shinepro` | `shine123` | Los Angeles, CA |
| Apex Detail Studio | `apexdetail` | `apex123` | Miami, FL |
| Velvet Auto Spa | `velvetauto` | `velvet123` | New York, NY |

Each studio also has a demo technician (e.g. `maria` / `tech123` for Shine Pro)
seeded into the `staff` table.

> ⚠️ After running `migrate_auth.py`, all plain-text passwords above are
> automatically rehashed to pbkdf2:sha256 on first successful login. The
> credentials themselves don't change — only how they're stored.

---

## Roles & permissions (`src/auth.py`)

| Role | Stored in | Access |
|---|---|---|
| **Owner / Admin** | `studios` table | Everything |
| **General Manager** | `staff.role` | Everything except staff management |
| **Service Advisor** | `staff.role` | Bookings, estimates, customers, payments |
| **Technician** | `staff.role` | Assigned jobs, media upload, client-visible notes only |
| **Photographer** | `staff.role` | Media gallery only |
| **Customer** | `customers` table | Own estimates / bookings only |

Decorators available for routes:
```python
@login_required              # any logged-in role
@admin_required               # admin or general_manager only
@staff_or_admin_required      # any staff role or admin — blocks customers
@roles_required("admin", "service_advisor")   # specific roles only
@permission_required("view_payments")          # fine-grained permission check
```

Fine-grained permissions matrix (`PERMISSIONS` dict in `auth.py`):
`view_internal_notes`, `edit_staff`, `delete_anything`, `view_all_jobs`,
`issue_warranty`, `view_payments`, `process_refund`, `upload_media`.

---

## Feature modules built so far

| Module | Routes | Notes |
|---|---|---|
| **Auth & login** | `/login`, `/logout` | Admin + Staff + Customer all log in here, role auto-detected |
| **Dashboard** | `/dashboard` | Stats (jobs, revenue, bookings today) + recent activity |
| **Bookings** | `/bookings`, `/bookings/new` | Service cards, bay/date picker, live slot availability (AJAX) |
| **Estimates** | `/estimates`, `/estimates/new`, `/estimates/<id>` | Line items, tax calc, customer approval link with digital signature |
| **Staff management** | `/staff`, `/staff/new` | Admin-only, role assignment, password strength validation |
| **Media gallery** | `/media`, `/media/upload` | Before/during/after photos & videos, per-job, role-gated uploads |
| **Customer CRM** | `/customers`, `/customers/new`, `/customers/<id>` | Multi-vehicle profiles, auto-created from bookings, full history |
| **Notes** | `/notes/add`, `/notes/<id>/delete` | Internal (staff-only) vs client-visible, threaded, reusable partial |
| **Follow-up reminders** | `/reminders`, `/reminders/new`, `/jobs/<id>/status` | Auto-scheduled on job completion (review + rebooking nudge), manual reminders |
| **AI Assistant** | `/assistant`, `/assistant/query` | Ollama primary, Gemini for research/vision, OpenAI optional fallback |

| **AI Estimator / Analytics** | `/analytics`, `/analytics/api/*` | Burn-rate forecasting, stockout dates, waste detection — Phase 1 |
| **Digital Vehicle Inspection** | `/dvi`, `/dvi/job/<id>`, `/dvi/inspection/<id>` | Photo capture → local vision model → priced upcharges → estimate — Phase 2 |
| **Dispatch Board** | `/dispatch`, `/dispatch/job/<id>` | Job/technician assignment, validated status transitions, audit trail — Phase 3 |

### Not yet built
- **Payments & Invoices** (Stripe test mode — deposits, partial payments, balance due, downloadable invoice, receipts)

---

## Architecture: the Orchestrator Pattern

Enforced layering. A violation of this is a bug, not a style preference:

```
routes / controllers  →  orchestrators  →  repositories  →  SQLite
                                      ↘   llm providers  →  Ollama (127.0.0.1)
```

**Controllers must never call an LLM.** They may import from
`services.orchestrators`. They may NOT import from `services.llm`,
`services.prompts`, or `requests`. Verify with:

```bash
grep -rn "OllamaBackend\|GeminiBackend\|requests.post" --include=*.py src/ | grep -v "^src/services/"
# must return nothing
```

```
src/services/
├── llm/                  Provider abstraction — the ONLY place that speaks HTTP to a model
│   ├── base.py           LLMProvider / VisionLLMProvider contracts, NullProvider, RecordingProvider
│   ├── ollama_provider.py  The single outbound call site in the whole AI stack
│   └── factory.py        Composition root — ask for a capability, not a vendor
├── repositories/         All SQL lives here, every query scoped by studio_id
├── analytics/            Deterministic forecasting — pure functions, no LLM, no I/O
├── pricing/              Deterministic defect → money mapping
├── prompts/              Prompt templates, versionable and diffable
└── orchestrators/        The only objects allowed to call a model
```

### The design rule that makes local models usable

**Python decides what is true. The model decides how to say it.**

A 3B/8B model on a laptop cannot reliably divide 4.5 litres by 0.31 litres/day —
ask it to, and it will confidently invent a date. So burn rates, stockout dates,
reorder quantities, condition scores and every price are computed in Python
*before* the model is consulted. The model receives finished arithmetic and is
explicitly forbidden from redoing it.

The consequence: **when Ollama is down you lose the prose, not the numbers.**
Every AI endpoint returns the same response shape either way, with
`_status.degraded` telling the UI whether to show a badge. The frontend never
branches on model availability.

Run the whole app with AI off to see this for yourself:

```bash
AI_ENABLED=0 python run.py     # every AI feature falls back to deterministic output
```

---

## Setup for Phases 1-3

```bash
# 1. Run the new migration (idempotent — safe to re-run)
python -m src.migrate_phase2

# 2. Optional: generate ~8 weeks of realistic inventory movement so the
#    forecaster has something to learn from on a fresh database
python -m src.migrate_phase2 --with-demo-data

# 3. Install the vision model for DVI (Phase 2)
ollama pull llava          # or llava:13b for noticeably better defect detection
ollama pull llama3.1:8b    # text model, if you don't have one

# 4. Run the tests — no Ollama required, the LLM is stubbed
python -m unittest discover tests -v
```

New `.env` keys (all optional, sensible defaults):

```
OLLAMA_VISION_MODEL=llava     # multimodal model used by DVI
OLLAMA_TEXT_TIMEOUT=90
OLLAMA_VISION_TIMEOUT=180     # vision is slow locally — be generous
AI_ENABLED=1                  # set to 0 to force every deterministic fallback
```

---

## Phase notes

### Phase 1 — AI Estimator / Analytics
`DepletionForecaster` blends a 7-day and 30-day window (weighted 60/40 toward
recent), adjusts for upcoming booking volume, and reports a confidence level
based on how much history actually exists. A product stocked three days ago is
divided by three days, not thirty. Today is excluded from the denominator until
it has usage logged — counting a partial day understates burn, and understating
burn tells a studio they have more time than they do.

Waste detection compares each job against **that studio's own median** for that
product, never an industry benchmark, and requires at least four jobs before it
will say anything. A large SUV legitimately uses more product; the output is
framed as "worth checking", never as an accusation, and never names a technician.

### Phase 2 — Digital Vehicle Inspection
One vision call per photo, not batched — local vision models bleed findings
between images when given several at once, and panel attribution matters when a
customer asks "where?".

The model works from a **closed defect vocabulary** (`DEFECT_TYPES` in
`services/prompts/dvi_prompts.py`). Anything outside it is dropped, not coerced:
a defect we can't price is a defect we can't bill for, and silently mapping it to
a neighbour would invent a charge. The prompt also states explicitly that an
empty findings list is a correct answer, which measurably reduces hallucinated
scratches on clean panels.

**The model never sees a price.** It reports type, panel and severity;
`UpchargeCalculator` prices it from the studio's own `services` table, falling
back to a rate card only when there's no match. Findings below 45% confidence are
recorded but never quoted.

### Phase 3 — Job & Technician Assignment
A validated state machine, not free-text status updates:

```
Pending ──→ In Progress ──→ Completed (terminal)
   ↑             │
   └─────────────┘
```

`In Progress` requires an assigned technician — which is exactly the assumption
the inventory scanner already makes when attributing material usage to a job.
Every transition and reassignment is appended to `job_status_history` /
`job_assignments`, so "who had this car and when" stays answerable.

Technician suggestions are deliberately **not** an LLM call: "who is free" is a
counting problem, a wrong answer misroutes real work, and a dispatcher should be
able to see the score and disagree with it.

---

## AI Assistant routing logic (`src/ai_service.py`)

```
Has image/video attachment, or question contains
"compare / worn / good / bad / looks like / show me"
        │
        ├─ Yes → Gemini (vision) → OpenAI → Ollama (best-effort fallback)
        │
        └─ No → contains "latest / trending / price of / research / market / ..."
                  │
                  ├─ Yes → Gemini → Ollama fallback
                  │
                  └─ No  → Ollama (default, free & fast) → Gemini fallback
```

Studio-specific context (recent jobs, services, bookings, estimates) is
automatically pulled from the DB and injected into every prompt.

---

## Database tables

| Table | Purpose |
|---|---|
| `studios` | Studio profile + admin login |
| `staff` | Staff logins, scoped role per studio |
| `customers` | Customer login + CRM profile |
| `vehicles` | One customer → many vehicles |
| `bays` | Physical bays per studio, used for slot booking |
| `services` | Service catalog per studio (name, price, duration) |
| `bookings` | Customer appointments, linked to service/bay/customer |
| `jobs` | Work items — car, service, status, technician, price |
| `estimates` / `estimate_items` | Quotes with line items, tax, approval + signature |
| `media` | Before/during/after photos & videos per job |
| `notes` | Internal vs client-visible notes, attachable to job/estimate/booking |
| `reminders` | Follow-up reminders — auto and manual, pending/sent/dismissed |
| `inventory_items` / `inventory_logs` | Stock levels and every movement — the AI Estimator's raw signal |
| `dvi_inspections` / `dvi_photos` / `dvi_findings` | Digital Vehicle Inspections; findings keep `raw_json` for dispute resolution |
| `job_assignments` / `job_status_history` | Dispatch audit trail |

---

## Common gotchas

- **`TemplateNotFound`** — almost always means `app.py`/`run.py` and `templates/`
  aren't in the same working directory. Run from the project root.
- **Stale session after a schema change** — visit `/logout` then log back in.
  Old session cookies don't have new fields and will raise `KeyError`.
- **`404` on a route that clearly exists** — usually a stale Flask process
  still bound to port 5055. Run `lsof -i :5055`, kill the old PID, restart.
- **Logo not showing** — filename in the `studios`/`staff` table must exactly
  match a file in `static/logos/` (including extension — `.svg` vs `.png`).
- **Merge conflicts on `src/config.py`** — never commit real API keys here.
  Keep them in `.env` (gitignored) and reference via `os.getenv(...)`.

---

## Git workflow

```bash
git add .
git commit -m "describe the change"
git push origin feature/vk-001-refactoringtofollowsolidprinciples
```

If push is rejected (teammate pushed first):
```bash
git config pull.rebase false   # use merge, not rebase
git pull origin feature/vk-001-refactoringtofollowsolidprinciples
# resolve any conflicts, then:
git add .
git commit -m "merge"
git push origin feature/vk-001-refactoringtofollowsolidprinciples
```
