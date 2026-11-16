# Deploying BayQ to production

This sets BayQ up on one Linux server (Ubuntu 22.04/24.04) behind nginx with
HTTPS. It takes about 30 minutes. The files it uses live in `deploy/`.

What you end up with:

```
customers ──https──▶ nginx :443 ──▶ gunicorn 127.0.0.1:8000 (3 workers) ──▶ SQLite (WAL)
                                                                        └─▶ Ollama 127.0.0.1:11434
```

The history of the earlier demo deployment is in
[docs/DEMO_DEPLOYMENT_LOG.md](docs/DEMO_DEPLOYMENT_LOG.md).

---

## 1. Server and domain

- A VM with **4+ CPU and 16+ GB RAM** if you run the AI models on it
  (llama3.1:8b + qwen2.5vl:7b). Without AI, 1 CPU / 2 GB is enough.
- A domain name pointing at the server (an `A` record), e.g. `bayq.example.com`.
- Firewall: open **22, 80 and 443** only. Never open 8000 (gunicorn) or 11434 (Ollama).

## 2. Install system packages

```bash
sudo apt update && sudo apt install -y python3.12 python3.12-venv nginx certbot python3-certbot-nginx git sqlite3
curl -fsSL https://ollama.com/install.sh | sh        # skip if AI_ENABLED=0
ollama pull llama3.1:8b && ollama pull qwen2.5vl:7b
```

## 3. Get the code

```bash
sudo useradd --system --create-home --home-dir /opt/bayq --shell /usr/sbin/nologin bayq
# Use the branch you are releasing (main once this work is merged).
sudo -u bayq git clone --branch feature/autofiera-stabilization https://github.com/ruthwiik8reddy/noname-app.git /opt/bayq/app-src
sudo -u bayq bash -c 'shopt -s dotglob && mv /opt/bayq/app-src/* /opt/bayq/ && rmdir /opt/bayq/app-src'
sudo -u bayq mkdir -p /opt/bayq/data /opt/bayq/logs
cd /opt/bayq
sudo -u bayq python3.12 -m venv .venv
sudo -u bayq .venv/bin/pip install -r requirements.txt
```

## 4. Configure

```bash
sudo -u bayq cp .env.example .env
sudo -u bayq chmod 600 .env
sudo -u bayq python3 -c "import secrets; print(secrets.token_hex(32))"   # paste as SECRET_KEY
sudo -u bayq nano .env
```

Set at least:

| Setting | Value |
|---|---|
| `APP_ENV` | `production` |
| `SECRET_KEY` | the 64-character value you just generated |
| `APP_BASE_URL` | `https://bayq.example.com` |
| `TRUSTED_PROXIES` | `1` |
| `DATABASE_URL` | `/opt/bayq/data/bayq.db` |
| `PRIVATE_UPLOAD_ROOT` | `/opt/bayq/data/private` |
| `SEED_DEMO` | `0` |
| `LLM_MAX_CONCURRENT` | `1` (× 3 workers = 3 AI calls at once) |

In production the app **refuses to start** if the secret key is weak, debug
or demo data is on, the public URL isn't https, or customer photos are still
in `static/uploads`.

**Moving an existing database?** Copy it to `/opt/bayq/data/bayq.db` (and old
photos with `python -m src.manage move-legacy-uploads`). The app upgrades the
schema on first start and keeps a `*-pre-repair-*.db.bak` copy if it had to
rebuild a table.

## 5. Create the first studio

```bash
cd /opt/bayq
sudo -u bayq bash -c 'set -a; . ./.env; set +a; .venv/bin/python -m src.manage create-studio \
    --name "Autofiera Detailing Studio" --city "Hyderabad" --owner "Owner Name" \
    --username autofiera --currency INR --country-code 91'
```

It asks for the owner's password (8+ characters with upper, lower, a number
and a symbol). Run it again for each studio.

## 6. Start the app

```bash
sudo cp deploy/bayq.service deploy/bayq-backup.service deploy/bayq-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bayq bayq-backup.timer
curl -s http://127.0.0.1:8000/healthz          # → {"status":"ok"}
```

## 7. nginx and HTTPS

```bash
sudo sed 's/bayq.example.com/YOUR.DOMAIN/g' deploy/nginx-bayq.conf | sudo tee /etc/nginx/sites-available/bayq
sudo ln -s /etc/nginx/sites-available/bayq /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo certbot --nginx -d YOUR.DOMAIN            # free certificate, auto-renews
sudo nginx -t && sudo systemctl reload nginx
```

## 8. Check

```bash
cd /opt/bayq && sudo -u bayq bash -c 'set -a; . ./.env; set +a; .venv/bin/python -m src.manage check'
```

Every line should show ✓ and end with **Ready.** Then open
`https://YOUR.DOMAIN`, sign in as the owner, and under **Settings** confirm
the currency and WhatsApp country code.

---

## Day-to-day operations

| Task | Command |
|---|---|
| Status / recent errors | `sudo systemctl status bayq` · `sudo journalctl -u bayq -n 100` |
| Restart | `sudo systemctl restart bayq` |
| Reload code with no downtime | `sudo systemctl reload bayq` |
| App log | `/opt/bayq/logs/app.log` (rotates at 10 MB × 5) |
| Find an error a user reported | search the log for the **Reference** shown on their error page |
| Backup now | `sudo systemctl start bayq-backup` (nightly at 02:30 anyway, last 14 kept in `data/backups`) |
| Reset a password | `… .venv/bin/python -m src.manage reset-password --username NAME` (signs them out everywhere) |
| Add a studio | `… .venv/bin/python -m src.manage create-studio --help` |

**Update to a new version**

```bash
cd /opt/bayq
sudo systemctl start bayq-backup                     # backup first
sudo -u bayq git pull
sudo -u bayq .venv/bin/pip install -r requirements.txt
sudo systemctl restart bayq                          # migrations run on start
```

**Restore a backup**

```bash
sudo systemctl stop bayq
sudo -u bayq cp /opt/bayq/data/backups/bayq-YYYYMMDD-HHMMSS.db /opt/bayq/data/bayq.db
sudo -u bayq rm -f /opt/bayq/data/bayq.db-wal /opt/bayq/data/bayq.db-shm
sudo systemctl start bayq
```

Backups stay on the same disk. Copy `data/backups/` off the server too (for
example a nightly `rclone` or `rsync` to another machine or bucket) so a lost
disk doesn't lose every studio's data.

---

## What protects the app

- **HTTPS only**: secure, HttpOnly, SameSite cookies; HSTS; http redirects to https.
- **Logins**: hashed passwords; lockout after 5 failures per IP and username,
  shared by all workers; nginx rate-limits `/login`; a password reset signs the
  account out everywhere; sessions expire after 8 hours.
- **Requests**: cross-site form posts are blocked; CSRF tokens on sensitive
  forms; Content-Security-Policy, X-Frame-Options and nosniff headers.
- **Data**: every query is scoped to the signed-in studio; customer photos and
  invoices are stored outside the web root and served only after a permission check.
- **Errors**: users see a friendly page with a reference number, never a traceback.

## Limits to know

- **One server.** SQLite in WAL mode with 3 workers handled 450 simultaneous
  writes in testing, which is plenty for dozens of studios. To run several
  servers behind a load balancer, move to PostgreSQL first.
- **AI is slow on CPU.** Vision checks take 30–180 s. Requests wait for a free
  slot rather than overloading Ollama; raise `LLM_MAX_CONCURRENT` only if the
  server has the RAM and cores.
