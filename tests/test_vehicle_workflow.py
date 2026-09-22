import unittest
from concurrent.futures import ThreadPoolExecutor
from werkzeug.datastructures import MultiDict
import test_job_records as fixtures
from src.services.repositories.vehicle_workflow import VehicleWorkflow
from src.services.repositories.job_records import RecordError


class VehicleWorkflowTests(unittest.TestCase):
    setUp=fixtures.JobRecordTests.setUp
    tearDown=fixtures.JobRecordTests.tearDown
    app=fixtures.JobRecordTests.app
    client=fixtures.JobRecordTests.client

    def prepare(self):
        self.conn.execute("INSERT INTO services(id,studio_id,name,price,duration_hr) VALUES(1,1,'Detail',150,2)")
        self.conn.execute("INSERT INTO bays(id,studio_id,name) VALUES(1,1,'Bay 1')")
        eid=self.conn.execute("""INSERT INTO estimates(studio_id,customer_id,customer_name,customer_email,customer_phone,vehicle,vehicle_id,status,subtotal,total,services_summary)
             VALUES(1,1,'Customer 1','','123','BMW',1,'Approved',12525,13778,'Approved detail')""").lastrowid
        self.conn.commit();self.workflow=VehicleWorkflow(connection=self.conn)
        return eid

    def form(self,**kw):
        f=dict(vehicle_id='1',service_id='1',bay_id='1',date='2026-09-25',time_slot='9:00 AM',request_key='booking-request-key-001')
        f.update(kw);return f

    def test_approved_estimate_booking_job_preserve_identity_and_cents(self):
        eid=self.prepare();bid=self.workflow.book(1,self.form(estimate_id=str(eid)),'manager')
        jid=self.workflow.convert_booking(1,bid,'manager');job=self.repo.detail(1,jid)['job']
        self.assertEqual((job['customer_id'],job['vehicle_id'],job['booking_id'],job['estimate_id']),(1,1,bid,eid))
        self.assertEqual(self.repo.detail(1,jid)['revenue_cents'],12525)
        self.assertEqual(job['service'],'Approved detail')
        timeline=self.repo.timeline(1,1)
        self.assertEqual(timeline['estimates'][0]['id'],eid);self.assertEqual(timeline['bookings'][0]['id'],bid)

    def test_booking_replay_and_changed_submission(self):
        self.prepare();f=self.form();bid=self.workflow.book(1,f,'manager')
        self.assertEqual(bid,self.workflow.book(1,f,'manager'))
        with self.assertRaises(RecordError):self.workflow.book(1,self.form(date='2026-09-26'),'manager')

    def test_job_conversion_is_concurrently_idempotent(self):
        self.prepare();bid=self.workflow.book(1,self.form(),'manager')
        def convert(_):return VehicleWorkflow(db_path=self.path).convert_booking(1,bid,'manager')
        with ThreadPoolExecutor(max_workers=2) as pool:ids=list(pool.map(convert,(1,2)))
        self.assertEqual(ids[0],ids[1])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM jobs WHERE booking_id=?',(bid,)).fetchone()[0],1)

    def test_quote_must_be_approved_and_tenant_scoped(self):
        eid=self.prepare()
        for status in ('Draft','Sent','Revision Requested'):
            self.conn.execute('UPDATE estimates SET status=? WHERE id=?',(status,eid));self.conn.commit()
            with self.assertRaises(RecordError):self.workflow.book(1,self.form(estimate_id=str(eid)),'manager')
        with self.assertRaises(RecordError):self.workflow.book(2,self.form(estimate_id=str(eid)),'manager')

    def test_foreign_vehicle_rejected_without_booking(self):
        self.prepare()
        with self.assertRaises(RecordError):self.workflow.book(1,self.form(vehicle_id='2'),'manager')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM bookings').fetchone()[0],0)

    def test_bay_conflict_rejects_equivalent_time_formats(self):
        self.prepare();self.workflow.book(1,self.form(),'manager')
        with self.assertRaises(RecordError):self.workflow.book(1,self.form(time_slot='09:00',request_key='another-booking-key-01'),'manager')

    def test_description_only_booking_never_guesses_vehicle(self):
        self.prepare();bid=self.workflow.book(1,self.form(vehicle_id='',customer_name='Customer 1',customer_phone='123',vehicle='BMW'),'manager')
        row=self.conn.execute('SELECT * FROM bookings WHERE id=?',(bid,)).fetchone()
        self.assertIsNone(row['vehicle_id'])
        with self.assertRaises(RecordError):self.workflow.convert_booking(1,bid,'manager')

    def test_cancelled_booking_cannot_create_job(self):
        self.prepare();bid=self.workflow.book(1,self.form(),'manager')
        self.conn.execute("UPDATE bookings SET status='Cancelled' WHERE id=?",(bid,));self.conn.commit()
        with self.assertRaises(RecordError):self.workflow.convert_booking(1,bid,'manager')

    def test_linked_job_cannot_drift_from_source(self):
        eid=self.prepare();bid=self.workflow.book(1,self.form(estimate_id=str(eid)),'manager')
        jid=self.workflow.convert_booking(1,bid,'manager')
        vid=self.conn.execute("INSERT INTO vehicles(studio_id,customer_id,make_model) VALUES(1,1,'BMW')").lastrowid;self.conn.commit()
        with self.assertRaises(RecordError):self.repo.link_vehicle(1,jid,vid,'manager')
        with self.assertRaises(RecordError):self.workflow.link_record(1,'bookings',bid,vid,'manager')
        with self.assertRaises(RecordError):self.workflow.link_record(1,'estimates',eid,vid,'manager')

    def test_creation_routes_validate_and_store_vehicle_id(self):
        self.prepare()
        from src.controller import bp as main
        from src.routes.job_record_routes import csrf_token
        app=self.app();app.register_blueprint(main);app.context_processor(lambda:{'work_records_csrf':csrf_token()})
        client=app.test_client()
        with client.session_transaction() as s:s.update(logged_in=True,role='admin',studio_id=1,user_id=1,work_records_csrf='token')
        data=MultiDict([('csrf','token'),('customer_id','1'),('vehicle_id','1'),('item_name','Detail'),('item_desc',''),('item_qty','1'),('item_price','100.25'),('tax_percent','0')])
        response=client.post('/estimates/new',data=data)
        self.assertEqual(response.status_code,302)
        est=self.conn.execute('SELECT * FROM estimates ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual(est['vehicle_id'],1);self.assertEqual(est['subtotal'],10025)
        data['vehicle_id']='2'
        self.assertEqual(client.post('/estimates/new',data=data).status_code,400)
        f=self.form();f['csrf']='token'
        self.assertEqual(client.post('/bookings/new',data=f).status_code,302)
        self.assertEqual(client.get('/bookings/new').status_code,200)
        self.assertEqual(client.get('/estimates/new').status_code,200)

    def test_legacy_unlinked_chain_can_be_verified_in_order(self):
        eid=self.prepare()
        self.conn.execute('UPDATE estimates SET vehicle_id=NULL WHERE id=?',(eid,))
        self.conn.execute("UPDATE customers SET phone='123' WHERE id=1");self.conn.commit()
        bid=self.workflow.book(1,self.form(vehicle_id='',customer_name='Customer 1',customer_phone='123',vehicle='BMW'),'manager')
        self.conn.execute('UPDATE bookings SET estimate_id=? WHERE id=?',(eid,bid));self.conn.commit()
        self.workflow.link_record(1,'estimates',eid,1,'manager')
        self.workflow.link_record(1,'bookings',bid,1,'manager')
        jid=self.workflow.convert_booking(1,bid,'manager')
        self.assertEqual(self.repo.detail(1,jid)['job']['vehicle_id'],1)

    def test_approved_vehicle_cannot_be_swapped_before_booking(self):
        eid=self.prepare()
        vid=self.conn.execute("INSERT INTO vehicles(studio_id,customer_id,make_model) VALUES(1,1,'BMW')").lastrowid;self.conn.commit()
        with self.assertRaises(RecordError):self.workflow.link_record(1,'estimates',eid,vid,'manager')

    def test_conversion_requires_csrf_and_manager(self):
        self.prepare();bid=self.workflow.book(1,self.form(),'manager')
        url=f'/work-records/bookings/{bid}/job'
        self.assertEqual(self.client().post(url).status_code,403)
        self.assertEqual(self.client('technician').post(url,data={'csrf':'token'}).status_code,403)
        self.assertEqual(self.client().post(url,data={'csrf':'token'}).status_code,303)


if __name__=='__main__':unittest.main()
