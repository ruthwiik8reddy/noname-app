"""SQLite transactional outbox: leased claims, coalescing, bounded retry and recovery."""
import time
import uuid
from ..repositories.base import BaseRepository

EVENT_AGENTS = {
    'jobs_changed': ('diagnosis','business'),
    'estimates_changed': ('estimate','business'),
    'booking_created': ('leads','business'),
    'stock_changed': ('inventory','business'),
    'leads_changed': ('leads','business'),
    'dvi_analyzed': ('estimate','business'),
    'customers_changed': ('business',),
}


class EventQueue(BaseRepository):
    def enqueue(self, studio_id, event):
        if event not in EVENT_AGENTS:
            raise ValueError('Unknown intelligence event')
        self.execute('''INSERT INTO intelligence_events(studio_id,event) VALUES(?,?)
            ON CONFLICT(studio_id,event) DO UPDATE SET revision=revision+1,
                attempts=0,available_at=0,updated_at=datetime('now')''',(studio_id,event))

    def retry_failed(self, studio_id):
        return self.execute('''UPDATE intelligence_events SET attempts=0,available_at=0,last_error=''
            WHERE studio_id=? AND attempts>=5 AND revision>processed_revision''',(studio_id,))

    def claim(self):
        now, token = time.time(), uuid.uuid4().hex
        with self._conn() as conn:
            with conn:
                row = conn.execute('''UPDATE intelligence_events SET token=?, lease_until=?
                    WHERE id=(SELECT id FROM intelligence_events WHERE revision>processed_revision
                     AND attempts<5 AND available_at<=? AND lease_until<=? ORDER BY updated_at,id LIMIT 1)
                    RETURNING *''', (token,now+1800,now,now)).fetchone()
                return dict(row) if row else None

    def finish(self, event, error=''):
        if error:
            self.execute('''UPDATE intelligence_events SET lease_until=0,token='',last_error=?,
                attempts=attempts+1,available_at=? WHERE id=? AND token=?''',
                (error[:400],time.time()+min(900,30*2**event['attempts']),event['id'],event['token']))
        else:
            self.execute('''UPDATE intelligence_events SET processed_revision=?,lease_until=0,
                token='',attempts=0,last_error='',available_at=0 WHERE id=? AND token=?''',
                (event['revision'],event['id'],event['token']))

    def drain(self, runner, limit=10):
        processed = 0
        for _ in range(limit):
            event = self.claim()
            if not event:
                break
            error = ''
            try:
                settings = runner.repo.settings(event['studio_id'])
                for name in EVENT_AGENTS.get(event['event'], ()):
                    if not settings.get(name,{}).get('enabled',True):
                        continue
                    result = runner.run_agent(event['studio_id'],name,trigger='event:'+event['event'])
                    if result is None or not result.ok:
                        raise RuntimeError('An agent failed or is already running; event will retry.')
            except Exception as exc:
                error = str(exc)
            self.finish(event,error)
            processed += 1
        return processed

    def status(self, studio_id):
        return self.fetch_all('''SELECT event,revision,processed_revision,attempts,last_error,updated_at
            FROM intelligence_events WHERE studio_id=? AND revision>processed_revision''',(studio_id,))
