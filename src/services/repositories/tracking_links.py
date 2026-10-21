"""Revocable, job-scoped bearer links. Raw tokens are never stored."""
import hashlib
import secrets
import time
from .base import BaseRepository
from .job_records import RecordError


class TrackingLinks(BaseRepository):
    def create(self, sid, jid, actor, now=None):
        now = int(time.time()) if now is None else now
        token = secrets.token_urlsafe(32)
        with self._conn() as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            job = conn.execute('SELECT * FROM jobs WHERE studio_id=? AND id=?', (sid, jid)).fetchone()
            if not job:
                raise RecordError('Job not found.')
            conn.execute('UPDATE job_tracking_links SET revoked_at=? WHERE studio_id=? AND job_id=? AND revoked_at IS NULL', (now, sid, jid))
            conn.execute('INSERT INTO job_tracking_links(studio_id,job_id,customer_id,vehicle_id,token_hash,expires_at,actor) VALUES(?,?,?,?,?,?,?)',
                         (sid, jid, job['customer_id'], job['vehicle_id'], hashlib.sha256(token.encode()).hexdigest(), now + 30*86400, actor))
        return token

    def revoke(self, sid, jid):
        self.execute('UPDATE job_tracking_links SET revoked_at=? WHERE studio_id=? AND job_id=? AND revoked_at IS NULL', (int(time.time()), sid, jid))

    def resolve(self, token, now=None):
        if len(token) != 43:
            return None
        now = int(time.time()) if now is None else now
        # Reassigning the job or vehicle invalidates previous recipients' links.
        return self.fetch_one("""SELECT j.id,j.car,j.service,j.status,j.completed_at,j.studio_id,
            COALESCE(j.tracking_stage,'') AS tracking_stage
            FROM job_tracking_links t JOIN jobs j ON j.id=t.job_id AND j.studio_id=t.studio_id
            LEFT JOIN vehicles v ON v.id=j.vehicle_id AND v.studio_id=j.studio_id
            WHERE t.token_hash=? AND t.revoked_at IS NULL AND t.expires_at>?
            AND j.customer_id IS t.customer_id AND j.vehicle_id IS t.vehicle_id
            AND (j.vehicle_id IS NULL OR v.customer_id IS j.customer_id)""",
            (hashlib.sha256(token.encode()).hexdigest(), now)) or None
