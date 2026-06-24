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

### Not yet built
- **Payments & Invoices** (Stripe test mode — deposits, partial payments, balance due, downloadable invoice, receipts)

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
