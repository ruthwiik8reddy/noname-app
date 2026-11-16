# BayQ — Oracle Cloud Deployment Log

A record of deploying BayQ (multi-tenant SaaS for auto detailing studios) to an
Oracle Cloud Free Tier VM for a YC application demo link.

**Live demo:** `http://150.136.219.119`

---

## 1. Application Overview

- **App:** BayQ — Flask app, SQLite database, Jinja2 templates.
- **AI:** Local Ollama integration (no cloud APIs).
  - Text model: `llama3.1:8b` (assistant, estimates, inventory analysis)
  - Vision model: `llava` (Digital Vehicle Inspection photo analysis)
- **Entry point:** `run.py` → `create_app()` → Flask on port `5055`.
- **Dependencies (`requirements.txt`):** Flask, requests, twilio, xhtml2pdf, python-dotenv.

The Ollama integration is fully implemented in code. When `AI_ENABLED=0`, AI
features degrade gracefully to deterministic fallbacks, so the app runs even
without a model.

---

## 2. Target Environment

- **Cloud:** Oracle Cloud Free Tier (Always Free eligible)
- **Region:** US East (Ashburn)
- **Shape:** `VM.Standard.A1.Flex` (ARM Ampere), 4 OCPU / 24 GB RAM
- **OS:** Ubuntu 22.04
- **Public IP:** `150.136.219.119`

The 24 GB RAM was chosen so both `llama3.1:8b` and `llava` fit comfortably.

---

## 3. Deployment Flow (Step by Step)

### Step 1 — Create the Compute Instance
- Compute → Instances → Create Instance
- Name: `bayq-demo`
- Image: Ubuntu 22.04
- Shape: `VM.Standard.A1.Flex` — 4 OCPU / 24 GB RAM (Always Free eligible)
- Capacity type: On-demand (default)
- Skipped Shielded Instance / Confidential Computing (hardware-level features,
  not needed for a demo).

### Step 2 — Networking (VCN)
- Created a new VCN `bayq-vcn` using the **VCN Wizard**
  ("Create VCN with Internet Connectivity") — auto-creates subnets, internet
  gateway, and route tables.
- Selected the VCN + its **public subnet** for the instance.
- Enabled **Automatically assign public IPv4 address**.

### Step 3 — SSH Key
- Generated a key pair during instance creation and downloaded the **private key**.
- Saved to: `C:\Users\Abhishek\Bayq-mukyam\ssh-key-2026-08-26.key`

### Step 4 — Connect via SSH
```powershell
ssh -i "C:\Users\Abhishek\Bayq-mukyam\ssh-key-2026-08-26.key" ubuntu@150.136.219.119
```
- Accepted the host fingerprint (`yes`) — added to `known_hosts`.

### Step 5 — Install System Dependencies
```bash
sudo apt update && sudo apt install -y python3 python3-pip python3-venv git curl unzip
```
- Prompted "Daemons using outdated libraries" → selected `<Ok>` to restart services.

### Step 6 — Open Port 80 in the OS Firewall
```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo netfilter-persistent save
```

### Step 7 — Install Ollama + Pull Models
```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.1:8b
ollama pull llava
```
- "No NVIDIA/AMD GPU detected" warning is expected — runs on CPU.
- Ollama runs as a systemd service on `127.0.0.1:11434`.

### Step 8 — Upload the Code (from laptop)
Run in a **local** PowerShell window (not the SSH session):
```powershell
Compress-Archive -Path "C:\Users\Abhishek\New folder\noname-app\*" -DestinationPath "$env:TEMP\bayq.zip" -Force
scp -i "C:\Users\Abhishek\Bayq-mukyam\ssh-key-2026-08-26.key" "$env:TEMP\bayq.zip" ubuntu@150.136.219.119:~/
```
Back on the VM:
```bash
mkdir ~/bayq && unzip ~/bayq.zip -d ~/bayq
```

### Step 9 — Python Environment
```bash
cd ~/bayq
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install gunicorn
```

### Step 10 — Create `.env`
```bash
cat > .env << 'EOF'
SECRET_KEY=bayq-yc-demo-2026-temp
FLASK_DEBUG=0
OLLAMA_URL=http://127.0.0.1:11434
OLLAMA_MODEL=llama3.1:8b
OLLAMA_VISION_MODEL=llava
AI_ENABLED=1
APP_BASE_URL=http://150.136.219.119
EOF
```

### Step 11 — Test Locally
```bash
python run.py    # confirmed "Running on http://127.0.0.1:5055", then Ctrl+C
```

### Step 12 — Run Publicly with Gunicorn
```bash
sudo ~/bayq/venv/bin/gunicorn --bind 0.0.0.0:80 --workers 2 --timeout 300 --chdir ~/bayq run:app --daemon
```

### Step 13 — Access
- Browser → `http://150.136.219.119`
- Demo logins (from seed data):

  | Username     | Password   |
  |--------------|------------|
  | `shinepro`   | `shine123` |
  | `apexdetail` | `apex123`  |
  | `velvetauto` | `velvet123`|

---

## 4. Problems Faced & How We Resolved Them

### Problem 1 — Public IP toggle disabled during instance creation
- **Symptom:** "Automatically assign public IPv4 address" toggle was greyed out,
  with a warning to select a public subnet.
- **Cause:** The inline "Create new public subnet" option in the instance wizard
  didn't reliably enable the public IP toggle.
- **Fix:** Created the VCN separately using the **VCN Wizard**
  ("Create VCN with Internet Connectivity"), then selected that existing VCN +
  public subnet in the instance wizard. Toggle became enabled.

### Problem 2 — "Out of capacity" on Create
- **Symptom:** Instance creation failed for the chosen Availability Domain.
- **Cause:** Free-tier ARM capacity is often exhausted in popular ADs.
- **Fix:** Retried with a different Availability Domain until it provisioned.

### Problem 3 — apt prompted to restart daemons
- **Symptom:** Purple "Daemons using outdated libraries" dialog.
- **Fix:** Tab to `<Ok>`, Enter. Normal post-update prompt.

### Problem 4 — Browser connection timed out on port 80
- **Symptom:** `http://150.136.219.119` timed out even though gunicorn was running.
- **Root cause (two layers):**
  1. **OS firewall (iptables):** Our ACCEPT rule for port 80 was placed at
     line 6, *after* a blanket `REJECT all` rule at line 5 — so traffic was
     rejected before reaching our rule.
  2. **Oracle VCN Security List:** The ingress rule was misconfigured with
     **Source Port = 80** and **Destination Port = All**, instead of
     Destination Port = 80.
- **Fix for iptables (move ACCEPT above REJECT):**
  ```bash
  sudo iptables -D INPUT 6
  sudo iptables -I INPUT 5 -m state --state NEW -p tcp --dport 80 -j ACCEPT
  sudo netfilter-persistent save
  ```
- **Fix for Security List:** Deleted the bad rule and added a correct one:
  - Source CIDR: `0.0.0.0/0`
  - IP Protocol: TCP
  - Source Port Range: (empty = All)
  - Destination Port Range: `80`
- **Verification used along the way:**
  ```bash
  sudo iptables -L INPUT -n --line-numbers   # confirm ACCEPT before REJECT
  sudo ss -tlnp | grep 80                     # confirm gunicorn listening on :80
  ps aux | grep gunicorn                       # confirm process running
  curl -I http://127.0.0.1:80                  # confirm app responds locally (302 → /login)
  ```
- **Outcome:** After fixing both layers, the site loaded.

### Problem 5 — Quick Quote: "Request failed (404)"
- **Symptom:** The Quick Quote / Generate Estimate feature returned a 404.
- **Diagnosis so far:**
  - Ollama itself is healthy — direct test succeeded:
    ```bash
    curl -s http://127.0.0.1:11434/api/generate -d '{"model":"llama3.1:8b","prompt":"hi","stream":false}'
    ```
  - `ollama list` shows `llama3.1:8b` and `llava:latest` installed.
  - `.env` values are correct.
  - Likely a missing/misnamed Flask route for the Quick Quote endpoint (not an
    Ollama problem). Route audit was the next step:
    ```bash
    python -c "from run import app; [print(r) for r in app.url_map.iter_rules()]" | grep -i quot
    ```
- **Status:** Open — rest of the app (dashboard, bookings, jobs, dispatch,
  customers, estimates, inventory, DVI) works.

---

## 5. Security Notes

For this **temporary YC demo**, deep hardening was intentionally deferred.
Known gaps to address if this becomes long-lived:

- No CSRF protection (no Flask-WTF)
- No rate limiting (no Flask-Limiter)
- No security headers / HTTPS enforcement (no Flask-Talisman, no TLS)
- Seed passwords are plaintext in `seed.py`
- Default `SECRET_KEY` in source (overridden via `.env` on the server)
- SQLite (fine for demo scale; not for production concurrency)
- Ollama port `11434` kept on localhost only — **must not** be exposed in the
  Oracle Security List.

Minimum steps taken for the demo: set a non-default `SECRET_KEY` in `.env`, and
only opened ports 22 (SSH) and 80 (HTTP) in the Security List.

---

## 6. Operations Cheat Sheet

**End the SSH session (VM keeps running):**
```bash
exit
```

**Reconnect:**
```powershell
ssh -i "C:\Users\Abhishek\Bayq-mukyam\ssh-key-2026-08-26.key" ubuntu@150.136.219.119
```

**Check if the app is running:**
```bash
ps aux | grep gunicorn
sudo ss -tlnp | grep 80
```

**Restart the app:**
```bash
cd ~/bayq
source venv/bin/activate
sudo ~/bayq/venv/bin/gunicorn --bind 0.0.0.0:80 --workers 2 --timeout 300 --chdir ~/bayq run:app --daemon
```

**Run in foreground to see errors live (debugging):**
```bash
sudo ~/bayq/venv/bin/gunicorn --bind 0.0.0.0:80 --workers 2 --timeout 300 --chdir ~/bayq run:app
```

**Check Ollama:**
```bash
ollama list
curl http://127.0.0.1:11434/api/tags
```

**App logs:**
```bash
cat ~/bayq/logs/app.log
```

**Clean up known_hosts on laptop after the demo (optional):**
```powershell
ssh-keygen -R 150.136.219.119
```
