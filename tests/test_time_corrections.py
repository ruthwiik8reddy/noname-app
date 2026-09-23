import unittest
from datetime import datetime,timezone
import test_job_records as fixtures
from src.services.repositories.job_records import RecordError


def stamp(value):return datetime.fromtimestamp(value,timezone.utc).isoformat()


class TimeCorrectionTests(unittest.TestCase):
    setUp=fixtures.JobRecordTests.setUp
    tearDown=fixtures.JobRecordTests.tearDown
    app=fixtures.JobRecordTests.app
    client=fixtures.JobRecordTests.client

    def timer(self):
        self.repo.set_rate(1,1,'30','owner')
        t=self.repo.start(1,1,1,'owner','original-timer-key-01',now=100)
        self.repo.stop(1,1,t,'owner',now=3700)
        return t

    def correct(self,t,start=100,end=1900,**kwargs):
        self.repo.correct_time(1,1,t,stamp(start),stamp(end),100,3700,'owner',kwargs.get('reason','Forgot to stop timer'),now=10000)

    def test_correction_recalculates_cost_clears_review_and_audits(self):
        t=self.timer();self.conn.execute("UPDATE jobs SET status='Completed' WHERE id=1");self.conn.commit();self.repo.review(1,1,'owner')
        self.correct(t)
        d=self.repo.detail(1,1);self.assertEqual(d['labor_cents'],1500);self.assertFalse(d['reviewed'])
        self.assertIn('previous_end',self.conn.execute("SELECT detail FROM job_record_audit WHERE action='time_corrected'").fetchone()[0])

    def test_stale_correction_rejected(self):
        t=self.timer();self.correct(t)
        with self.assertRaises(RecordError):self.correct(t,end=2800)

    def test_future_reversed_empty_reason_and_foreign_rejected(self):
        t=self.timer()
        for start,end,reason in [(100,11000,'future'),(500,100,'reversed'),(100,200,'')]:
            with self.assertRaises(RecordError):self.correct(t,start,end,reason=reason)
        with self.assertRaises(RecordError):self.repo.correct_time(2,1,t,stamp(100),stamp(200),100,3700,'foreign','reason')

    def test_overlap_rejected(self):
        t=self.timer();other=self.repo.start(1,1,1,'owner','second-timer-key-01',now=4000)
        self.repo.stop(1,1,other,'owner',now=5000)
        with self.assertRaises(RecordError):self.correct(t,end=4500)

    def test_running_timer_and_technician_correction_rejected(self):
        t=self.repo.start(1,1,1,'owner','running-timer-key-01',now=100)
        with self.assertRaises(RecordError):self.correct(t)
        response=self.client('technician').post('/work-records/jobs/1/correct-time',data={'csrf':'token','timer_id':t})
        self.assertEqual(response.status_code,403)
