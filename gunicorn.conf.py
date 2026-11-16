"""
Gunicorn settings for production: `gunicorn -c gunicorn.conf.py run:app`.

Every value can be overridden with an environment variable of the same name
in upper case (e.g. GUNICORN_WORKERS=4) so the systemd unit stays generic.
"""

import os

# Only nginx talks to gunicorn; it must not be reachable from outside.
bind = os.getenv("GUNICORN_BIND", "127.0.0.1:8000")

# Threads, not just processes: requests that wait on the local AI model
# (up to OLLAMA_VISION_TIMEOUT seconds) shouldn't block everyone else.
worker_class = "gthread"
workers = int(os.getenv("GUNICORN_WORKERS", "3"))
threads = int(os.getenv("GUNICORN_THREADS", "4"))

# Longer than the slowest AI call (vision: 180 s by default).
timeout = int(os.getenv("GUNICORN_TIMEOUT", "300"))
graceful_timeout = 30
keepalive = 5

# Recycle workers now and then so slow memory growth can't accumulate.
max_requests = 2000
max_requests_jitter = 200

# Logs go to journald via stdout/stderr; the app also writes logs/app.log.
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info")
# Request ID lets a user's "Reference: …" on an error page be found here.
access_log_format = '%({x-forwarded-for}i)s "%(r)s" %(s)s %(b)s %(M)sms ref=%({x-request-id}o)s'

# Don't leak the server software/version.
proc_name = "bayq"
