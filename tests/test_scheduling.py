import unittest
from concurrent.futures import ThreadPoolExecutor
import test_vehicle_workflow as fixtures
from src.services.repositories.vehicle_workflow import VehicleWorkflow
from src.services.repositories.job_records import RecordError
from src.services.repositories.scheduling import duration_minutes


class SchedulingTests(unittest.TestCase):
    setUp=fixtures.VehicleWorkflowTests.setUp
    tearDown=fixtures.VehicleWorkflowTests.tearDown
    app=fixtures.VehicleWorkflowTests.app
    prepare=fixtures.VehicleWorkflowTests.prepare
    form=fixtures.VehicleWorkflowTests.form

    def book(self,**kw):return self.workflow.book(1,self.form(**kw),'owner')

    def test_partial_overlap_rejected_and_back_to_back_allowed(self):
        self.prepare();self.book()
        with self.assertRaises(RecordError):self.book(time_slot='10:00 AM',request_key='overlap-key-00001')
        self.assertTrue(self.book(time_slot='11:00 AM',request_key='adjacent-key-0001'))

    def test_duration_snapshot_survives_service_edit(self):
        self.prepare();bid=self.book()
        self.conn.execute('UPDATE services SET duration_hr=0.5 WHERE id=1');self.conn.commit()
        with self.assertRaises(RecordError):self.book(time_slot='10:00 AM',request_key='snapshot-key-0001')
        self.assertEqual(self.conn.execute('SELECT duration_minutes FROM bookings WHERE id=?',(bid,)).fetchone()[0],120)

    def test_overnight_overlap_and_containment(self):
        self.prepare();self.book(time_slot='11:00 PM')
        with self.assertRaises(RecordError):self.book(date='2026-09-26',time_slot='12:00 AM',request_key='midnight-key-0001')
        with self.assertRaises(RecordError):self.book(time_slot='10:00 PM',request_key='container-key-01')

    def test_cancel_reactivate_checks_conflicts_and_audit(self):
        self.prepare();bid=self.book()
        self.workflow.reservation_status(1,bid,'Cancelled','owner')
        self.book(time_slot='10:00 AM',request_key='replacement-key-01')
        with self.assertRaises(RecordError):self.workflow.reservation_status(1,bid,'Confirmed','owner')
        self.assertEqual(self.conn.execute('SELECT status FROM bookings WHERE id=?',(bid,)).fetchone()[0],'Cancelled')

    def test_concurrent_overlapping_reservations_only_one_wins(self):
        self.prepare()
        def book(i):
            try:return VehicleWorkflow(db_path=self.path).book(1,self.form(time_slot=f'{9+i}:00 AM',request_key=f'concurrent-key-000{i}'),'owner')
            except RecordError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(book,(0,1)))
        self.assertEqual(sum(r is not None for r in results),1)

    def test_invalid_durations_fail_closed(self):
        for value in (0,-1,'NaN','Infinity',200):
            with self.assertRaises(RecordError):duration_minutes(value)
        self.assertEqual(duration_minutes('0.333333'),20)

    def test_other_bay_and_studio_independent(self):
        self.prepare();self.book()
        self.conn.execute("INSERT INTO bays(id,studio_id,name) VALUES(2,1,'Bay 2')");self.conn.commit()
        self.assertTrue(self.book(bay_id='2',request_key='another-bay-key-1'))
        with self.assertRaises(RecordError):self.workflow.reservation_status(2,1,'Cancelled','foreign')

    def test_legacy_duration_is_checked(self):
        self.prepare();bid=self.book()
        self.conn.execute('UPDATE bookings SET duration_minutes=NULL WHERE id=?',(bid,));self.conn.commit()
        with self.assertRaises(RecordError):self.book(time_slot='10:00 AM',request_key='legacy-overlap-01')

    def test_status_route_csrf_and_availability_full_duration(self):
        self.prepare();bid=self.book()
        from src.controller import bp
        app=self.app();app.register_blueprint(bp);client=app.test_client()
        with client.session_transaction() as s:s.update(logged_in=True,studio_id=1,role='admin',user_id=1,work_records_csrf='token')
        self.assertEqual(client.post(f'/bookings/{bid}/status',data={'status':'Cancelled'}).status_code,403)
        slots=client.get('/bookings/slots?date=2026-09-25&bay_id=1&service_id=1')
        self.assertEqual(slots.status_code,200);self.assertIn('10:00 AM',slots.json['booked'])
        self.assertNotIn('11:00 AM',slots.json['booked'])
        self.assertEqual(client.post(f'/bookings/{bid}/status',data={'csrf':'token','status':'Cancelled'}).status_code,303)
