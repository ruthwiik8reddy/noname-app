import unittest
from concurrent.futures import ThreadPoolExecutor
import test_job_records as fixtures
from src.services.repositories.followups import Followups
from src.services.repositories.job_records import RecordError
from src.migrate_followups import migrate


class FollowupTests(unittest.TestCase):
    tearDown=fixtures.JobRecordTests.tearDown
    client=fixtures.JobRecordTests.client

    def setUp(self):
        fixtures.JobRecordTests.setUp(self)
        self.conn.execute("UPDATE customers SET email='customer@example.test',phone='+14155550123' WHERE id=1")
        self.conn.execute("UPDATE jobs SET status='Completed',vehicle_id=1,customer_id=1,completed_at='2025-01-01' WHERE id=1")
        self.conn.execute("INSERT INTO services(id,studio_id,name,duration_hr,price) VALUES(1,1,'Detail',2,150)")
        self.conn.commit();self.followups=Followups(connection=self.conn)

    def app(self):
        app=fixtures.JobRecordTests.app(self)
        from src.routes.followup_routes import bp
        app.register_blueprint(bp);return app

    def draft(self,key='followup-request-key-01',channel='email'):
        return self.followups.create(1,1,'rebooking',1,channel,'owner',key)

    def version(self,fid):
        return self.followups.detail(1,fid)['record']['version']

    def change(self,fid,operation,**kw):
        return self.followups.change(1,fid,operation,self.version(fid),'owner',**kw)

    def allow(self,channel='email'):
        self.followups.permission(1,1,channel,'yes','Customer opted in on signed demo form','owner')

    def contact(self,fid):
        self.allow();self.change(fid,'approve');self.change(fid,'contacted',confirmed='yes',reference='Synthetic external contact log')

    def booking(self,vid=1,cid=1,when='2026-01-01',created=None):
        bid=self.conn.execute("""INSERT INTO bookings(studio_id,customer_name,customer_phone,vehicle,vehicle_id,customer_id,service_id,date,time_slot)
              VALUES(1,'Customer','+14155550123','BMW',?,?,1,?,'9:00 AM')""",(vid,cid,when)).lastrowid
        if created:self.conn.execute('UPDATE bookings SET created_at=? WHERE id=?',(created,bid))
        self.conn.commit();return bid

    def test_candidate_uses_verified_old_visit(self):
        self.assertEqual(self.followups.candidates(1)[0]['source_id'],1)
        self.conn.execute('UPDATE jobs SET vehicle_id=NULL WHERE id=1');self.conn.commit()
        self.assertEqual(self.followups.candidates(1),[])

    def test_new_visit_and_future_booking_suppress_candidate(self):
        self.booking(when='2099-01-01')
        self.assertEqual(self.followups.candidates(1),[])
        fid=self.draft();self.allow()
        with self.assertRaises(RecordError):self.change(fid,'approve')

    def test_draft_can_exist_without_permission_but_not_approval(self):
        fid=self.draft()
        self.assertIn('permission',self.followups.detail(1,fid)['blocked'])
        with self.assertRaises(RecordError):self.change(fid,'approve')
        self.allow();self.change(fid,'approve')
        self.assertEqual(self.followups.detail(1,fid)['record']['status'],'Approved')

    def test_replay_and_changed_request_key(self):
        fid=self.draft();self.assertEqual(fid,self.draft())
        with self.assertRaises(RecordError):self.draft(channel='sms')
        with self.assertRaises(RecordError):self.draft(key='different-request-key-02')

    def test_concurrent_replay_creates_one_draft(self):
        def create(_):return Followups(db_path=self.path).create(1,1,'rebooking',1,'email','owner','same-request-key-01')
        with ThreadPoolExecutor(max_workers=2) as pool:ids=list(pool.map(create,(1,2)))
        self.assertEqual(ids[0],ids[1])

    def test_edit_revokes_approval_and_stale_version_rejected(self):
        fid=self.draft();self.allow();self.change(fid,'approve');version=self.version(fid)
        self.change(fid,'edit',body='Reviewed message')
        self.assertEqual(self.followups.detail(1,fid)['record']['status'],'Draft')
        with self.assertRaises(RecordError):self.followups.change(1,fid,'approve',version,'owner')

    def test_optout_revokes_approval_and_cannot_be_bypassed(self):
        fid=self.draft();self.allow();self.change(fid,'approve')
        self.followups.permission(1,1,'email','no','Customer requested opt-out','owner')
        with self.assertRaises(RecordError):self.change(fid,'contacted',confirmed='yes',reference='no')
        with self.assertRaises(RecordError):self.change(fid,'approve')

    def test_permission_is_destination_and_channel_specific(self):
        fid=self.draft(channel='sms');self.allow('email')
        with self.assertRaises(RecordError):self.change(fid,'approve')
        self.allow('sms');self.change(fid,'approve')
        self.conn.execute("UPDATE customers SET phone='+14155550999' WHERE id=1");self.conn.commit()
        with self.assertRaises(RecordError):self.change(fid,'contacted',confirmed='yes',reference='external')
        self.change(fid,'edit',body='Updated recipient')
        with self.assertRaises(RecordError):self.change(fid,'approve')
        self.allow('sms');self.change(fid,'approve')

    def test_optout_works_after_contact_removed(self):
        self.allow();self.conn.execute("UPDATE customers SET email='' WHERE id=1");self.conn.commit()
        self.followups.permission(1,1,'email','no','Opt-out request','owner')
        self.assertEqual(self.conn.execute('SELECT allowed FROM contact_permissions').fetchone()[0],0)

    def test_external_contact_requires_approval_and_attestation(self):
        fid=self.draft()
        with self.assertRaises(RecordError):self.change(fid,'contacted',confirmed='yes',reference='external')
        self.allow();self.change(fid,'approve')
        with self.assertRaises(RecordError):self.change(fid,'contacted',reference='external')
        self.change(fid,'contacted',confirmed='yes',reference='external')
        with self.assertRaises(RecordError):self.change(fid,'edit',body='changed')

    def test_booking_outcome_is_verified_and_audited(self):
        fid=self.draft();self.contact(fid);bid=self.booking()
        self.change(fid,'outcome',booking_id=str(bid),reason='Customer confirmed response')
        result=self.followups.detail(1,fid)
        self.assertEqual(result['record']['booking_id'],bid)
        self.assertEqual(result['record']['status'],'Closed')
        self.assertEqual(result['history'][0]['action'],'outcome')

    def test_preexisting_booking_same_second_not_attributed(self):
        bid=self.booking();fid=self.draft();self.contact(fid)
        with self.assertRaises(RecordError):self.change(fid,'outcome',booking_id=bid,reason='incorrect')

    def test_wrong_customer_cancelled_and_old_booking_rejected(self):
        fid=self.draft();self.contact(fid)
        for bid in (self.booking(cid=2),self.booking(created='2020-01-01')):
            with self.assertRaises(RecordError):self.change(fid,'outcome',booking_id=bid,reason='incorrect')
        bid=self.booking();self.conn.execute("UPDATE bookings SET status='Cancelled' WHERE id=?",(bid,));self.conn.commit()
        with self.assertRaises(RecordError):self.change(fid,'outcome',booking_id=bid,reason='incorrect')

    def test_closed_and_recent_followups_suppressed(self):
        fid=self.draft();self.assertEqual(self.followups.candidates(1),[])
        self.change(fid,'close',reason='Not needed')
        self.assertEqual(self.followups.candidates(1),[])
        with self.assertRaises(RecordError):self.change(fid,'approve')

    def test_invalid_contact_and_source_rejected(self):
        self.conn.execute("UPDATE customers SET phone='5551234' WHERE id=1");self.conn.commit()
        with self.assertRaises(RecordError):self.draft(channel='sms')
        with self.assertRaises(RecordError):self.followups.create(1,2,'rebooking',2,'email','owner','request-key-foreign')
        with self.assertRaises(RecordError):self.followups.create(1,1,'estimate',1,'email','owner','request-key-bad-src')

    def test_routes_csrf_role_tenant_and_escaping(self):
        fid=self.draft();client=self.client()
        self.assertEqual(client.get('/followups/').status_code,200)
        self.assertEqual(client.get(f'/followups/{fid}').status_code,200)
        self.assertEqual(self.client('technician').get('/followups/').status_code,403)
        self.assertEqual(client.post(f'/followups/{fid}/approve').status_code,403)
        self.assertEqual(client.post('/followups/999/permission',data={'csrf':'token'}).status_code,404)
        self.change(fid,'edit',body='<script>alert(1)</script>')
        self.assertNotIn(b'<script>alert(1)</script>',client.get(f'/followups/{fid}').data)
        with client.session_transaction() as s:s['studio_id']=2
        self.assertEqual(client.get(f'/followups/{fid}').status_code,404)
        self.assertEqual(client.post(f'/followups/{fid}/approve',data={'csrf':'token'}).status_code,404)

    def test_permission_route_persists_preference(self):
        fid=self.draft()
        r=self.client().post(f'/followups/{fid}/permission',data={'csrf':'token','allowed':'yes','evidence':'Signed form'})
        self.assertEqual(r.status_code,303)
        self.assertIsNone(self.followups.detail(1,fid)['blocked'])

    def test_migration_repeatable(self):
        fid=self.draft();migrate(self.path);migrate(self.path)
        self.assertEqual(self.followups.detail(1,fid)['record']['id'],fid)

    def test_vehicle_timeline_contains_only_its_followups(self):
        fid=self.draft()
        self.assertEqual(self.repo.timeline(1,1)['followups'][0]['id'],fid)
        self.assertEqual(self.repo.timeline(2,2)['followups'],[])
