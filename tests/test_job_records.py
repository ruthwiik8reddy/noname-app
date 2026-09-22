import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from flask import Flask
from src.config import Config
from src.db_manager import init_db,close_db
from src.services.repositories.job_records import JobRecords,RecordError


class JobRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=os.path.join(self.temp.name,'app.sqlite3')
        self.config=patch.object(Config,'DB_PATH',self.path);self.config.start();init_db()
        self.conn=sqlite3.connect(self.path);self.conn.row_factory=sqlite3.Row
        for sid in (1,2):
            self.conn.execute("INSERT INTO studios(id,name,city,owner,logo,username,password) VALUES(?,?,'City','Owner','',?,'test')",(sid,f'Studio {sid}',f'owner{sid}'))
            self.conn.execute("INSERT INTO customers(id,studio_id,name) VALUES(?,?,?)",(sid,sid,f'Customer {sid}'))
            self.conn.execute("INSERT INTO staff(id,studio_id,name,role,username,password) VALUES(?,?,?,'technician',?,'test')",(sid,sid,f'Tech {sid}',f'tech{sid}'))
            self.conn.execute("INSERT INTO vehicles(id,studio_id,customer_id,make_model,license_plate) VALUES(?,?,?,'BMW','TEST')",(sid,sid,sid))
            self.conn.execute("INSERT INTO jobs(id,studio_id,car,service,status,price,assigned_staff_id) VALUES(?,?,'BMW','Detail','In Progress',200,?)",(sid,sid,sid))
            self.conn.execute("INSERT INTO inventory_items(id,studio_id,sku,name,quantity,cost_per_unit) VALUES(?,?,?,'Coating',10,250)",(sid,sid,f'ITEM{sid}'))
        self.conn.commit();self.repo=JobRecords(connection=self.conn)

    def tearDown(self):
        self.conn.close();self.config.stop();self.temp.cleanup()

    def use(self,qty='2',key='consumption-key-00001',**kw):
        return self.repo.material(1,1,1,qty,'manager',key,**kw)

    def test_vehicle_link_records_customer_and_audit(self):
        self.repo.link_vehicle(1,1,1,'manager');d=self.repo.detail(1,1)
        self.assertEqual(d['job']['customer_id'],1);self.assertEqual(d['vehicle']['id'],1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM job_record_audit WHERE action='vehicle_linked'").fetchone()[0],1)

    def test_never_guesses_vehicle_from_matching_name(self):
        self.assertIsNone(self.repo.detail(1,1)['vehicle'])
        self.assertEqual(self.repo.timeline(1,1)['jobs'],[])

    def test_cross_studio_vehicle_and_customer_mismatch_rejected(self):
        with self.assertRaises(RecordError):self.repo.link_vehicle(1,1,2,'manager')
        self.conn.execute('UPDATE jobs SET customer_id=2 WHERE id=1');self.conn.commit()
        with self.assertRaises(RecordError):self.repo.link_vehicle(1,1,1,'manager')
        self.assertIsNone(self.repo.detail(1,1)['vehicle'])

    def test_timeline_only_explicit_links(self):
        self.repo.link_vehicle(1,1,1,'manager')
        self.assertEqual([j['id'] for j in self.repo.timeline(1,1)['jobs']],[1])
        with self.assertRaises(RecordError):self.repo.timeline(1,2)

    def test_timer_cost_snapshot_not_repriced_by_new_rate(self):
        self.repo.set_rate(1,1,'30','manager')
        timer=self.repo.start(1,1,1,'tech','timer-request-key-1',now=100)
        self.repo.set_rate(1,1,'50','manager');self.repo.stop(1,1,timer,'tech',1,now=1900)
        d=self.repo.detail(1,1);self.assertEqual(d['seconds'],1800);self.assertEqual(d['labor_cents'],1500)

    def test_duplicate_timer_start_and_stop_are_idempotent(self):
        t=self.repo.start(1,1,1,'tech','timer-request-key-1',now=100)
        self.assertEqual(t,self.repo.start(1,1,1,'tech','timer-request-key-1',now=200))
        self.repo.stop(1,1,t,'tech',1,now=300);self.repo.stop(1,1,t,'tech',1,now=500)
        self.assertEqual(self.repo.detail(1,1)['seconds'],200)

    def test_one_active_timer_per_staff(self):
        self.repo.start(1,1,1,'tech','timer-request-key-1',now=100)
        with self.assertRaises(RecordError):self.repo.start(1,1,1,'tech','timer-request-key-2',now=200)

    def test_foreign_staff_cannot_start_or_stop(self):
        with self.assertRaises(RecordError):self.repo.start(1,1,2,'tech','timer-request-key-1')
        t=self.repo.start(1,1,1,'tech','timer-request-key-1')
        with self.assertRaises(RecordError):self.repo.stop(1,1,t,'other',2)

    def test_completion_stops_timer_even_through_legacy_sql(self):
        self.repo.start(1,1,1,'tech','timer-request-key-1')
        self.conn.execute("UPDATE jobs SET status='Completed' WHERE id=1");self.conn.commit()
        self.assertEqual(self.repo.detail(1,1)['running'],0)
        with self.assertRaises(RecordError):self.repo.start(1,1,1,'tech','timer-request-key-2')

    def test_missing_rate_is_unknown_not_free_labor(self):
        t=self.repo.start(1,1,1,'tech','timer-request-key-1',now=100)
        self.repo.stop(1,1,t,'tech',now=3700)
        d=self.repo.detail(1,1);self.assertEqual(d['unknown_costs'],1);self.assertIsNone(d['contribution_cents'])
        self.repo.correct_cost(1,1,'labor',t,'30','manager','Verified hourly cost')
        self.assertEqual(self.repo.detail(1,1)['labor_cents'],3000)

    def test_stock_consumption_and_ledger_are_atomic(self):
        self.use('2.5');d=self.repo.detail(1,1)
        self.assertEqual(d['material_cents'],625)
        self.assertEqual(self.conn.execute('SELECT quantity FROM inventory_items WHERE id=1').fetchone()[0],7.5)
        self.assertEqual(self.conn.execute('SELECT change_qty FROM inventory_logs WHERE item_id=1').fetchone()[0],-2.5)

    def test_overspending_stock_writes_nothing(self):
        with self.assertRaises(RecordError):self.use('11')
        self.assertEqual(self.conn.execute('SELECT quantity FROM inventory_items WHERE id=1').fetchone()[0],10)
        self.assertEqual(self.repo.detail(1,1)['materials'],[])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM inventory_logs').fetchone()[0],0)

    def test_nonfinite_negative_and_excess_precision_rejected(self):
        for qty in ('NaN','Infinity','-1','0','0.0001'):
            with self.subTest(qty=qty),self.assertRaises(RecordError):self.use(qty)

    def test_material_request_replay_does_not_double_deduct(self):
        first=self.use();self.assertEqual(self.use(),first)
        with self.assertRaises(RecordError):self.use('3')
        self.assertEqual(self.conn.execute('SELECT quantity FROM inventory_items WHERE id=1').fetchone()[0],8)

    def test_cross_studio_item_rejected(self):
        with self.assertRaises(RecordError):self.repo.material(1,1,2,'1','manager','request-material-1')

    def test_return_uses_original_cost_not_current_catalog(self):
        first=self.use()
        self.conn.execute('UPDATE inventory_items SET cost_per_unit=900 WHERE id=1');self.conn.commit()
        self.use('1',key='return-request-key-1',return_of=first,reason='Unused')
        self.assertEqual(self.repo.detail(1,1)['material_cents'],250)
        with self.assertRaises(RecordError):self.use('2',key='return-request-key-2',return_of=first,reason='Unused')

    def test_small_partial_returns_never_refund_more_than_consumed(self):
        self.conn.execute('UPDATE inventory_items SET cost_per_unit=600 WHERE id=1');self.conn.commit()
        first=self.use('0.004')
        for i in range(4):
            self.use('0.001',key=f'return-request-key-{i}',return_of=first,reason='Unused')
            self.assertGreaterEqual(self.repo.detail(1,1)['material_cents'],0)
        self.assertEqual(self.repo.detail(1,1)['material_cents'],0)

    def test_cost_correction_recalculates_returns_without_stock_change(self):
        first=self.use();self.use('1',key='return-request-key-1',return_of=first,reason='Unused')
        self.repo.correct_cost(1,1,'material',first,'5','manager','Verified supplier cost')
        self.assertEqual(self.repo.detail(1,1)['material_cents'],500)
        self.assertEqual(self.conn.execute('SELECT quantity FROM inventory_items WHERE id=1').fetchone()[0],9)

    def test_review_is_explicit_and_invalidated_by_changes(self):
        self.use();self.repo.plan(1,1,'60','10','manager')
        with self.assertRaises(RecordError):self.repo.review(1,1,'manager')
        self.conn.execute("UPDATE jobs SET status='Completed' WHERE id=1");self.conn.commit()
        self.repo.review(1,1,'manager');self.assertTrue(self.repo.detail(1,1)['reviewed'])
        self.use('1',key='new-consumption-key')
        self.assertFalse(self.repo.detail(1,1)['reviewed'])

    def test_concurrent_consumption_cannot_overdraw(self):
        def consume(n):
            try:
                JobRecords(db_path=self.path).material(1,1,1,'7','manager',f'concurrent-key-{n:04}')
                return True
            except RecordError:return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(consume,(1,2)))
        self.assertEqual(sum(results),1)
        self.assertEqual(self.conn.execute('SELECT quantity FROM inventory_items WHERE id=1').fetchone()[0],3)

    def test_intelligence_only_uses_reviewed_costs(self):
        from datetime import date
        from src.services.intelligence.facts import IntelligenceFacts
        self.use('2')
        self.conn.execute("UPDATE jobs SET status='Completed',completed_at='2026-09-22' WHERE id=1");self.conn.commit()
        def fact():
            data=IntelligenceFacts(connection=self.conn).snapshot(1,today=date(2026,9,22))
            return next(f for f in data['facts'] if f['id']=='reviewed_contribution')
        self.assertIsNone(fact()['value'])
        self.repo.review(1,1,'manager')
        self.assertEqual(fact()['value'],19500)
        self.use('1',key='new-consumption-key')
        self.assertIsNone(fact()['value'])

    def test_repeated_migration_preserves_records(self):
        from src.migrate_job_records import migrate
        self.use();migrate(self.path);migrate(self.path)
        self.assertEqual(len(self.repo.detail(1,1)['materials']),1)

    def test_cancelled_job_stops_timer_and_rate_correction_clears_review(self):
        t=self.repo.start(1,1,1,'tech','timer-request-key-1',now=100)
        self.conn.execute("UPDATE jobs SET status='Cancelled' WHERE id=1");self.conn.commit()
        self.assertEqual(self.repo.detail(1,1)['running'],0)
        self.repo.correct_cost(1,1,'labor',t,'0','manager','No charge owner time')
        self.conn.execute("UPDATE jobs SET status='Completed' WHERE id=1");self.conn.commit()
        self.repo.review(1,1,'manager');self.assertTrue(self.repo.detail(1,1)['reviewed'])
        self.repo.correct_cost(1,1,'labor',t,'10','manager','Corrected labor cost')
        self.assertFalse(self.repo.detail(1,1)['reviewed'])

    def app(self):
        from src.routes.job_record_routes import bp
        app=Flask(__name__,template_folder=os.path.abspath('templates'));app.secret_key='test';app.config['TESTING']=True
        app.register_blueprint(bp);app.teardown_appcontext(close_db)
        return app

    def client(self,role='admin',staff=1):
        client=self.app().test_client()
        with client.session_transaction() as s:s.update(logged_in=True,studio_id=1,role=role,staff_id=staff,user_id=1,work_records_csrf='token')
        return client

    def test_page_renders_empty_records_and_hides_costs_from_technician(self):
        owner=self.client().get('/work-records/jobs/1');self.assertEqual(owner.status_code,200)
        tech=self.client('technician').get('/work-records/jobs/1')
        self.assertEqual(tech.status_code,200);self.assertNotIn(b'Job contribution',tech.data)
        self.assertNotIn(b'Set a staff cost rate',tech.data)

    def test_csrf_and_roles_guard_writes(self):
        self.assertEqual(self.client().post('/work-records/jobs/1/consume').status_code,403)
        self.assertEqual(self.client('technician').post('/work-records/jobs/1/rate',data={'csrf':'token'}).status_code,403)
        self.assertEqual(self.client('technician',staff=2).get('/work-records/jobs/1').status_code,403)
        self.assertEqual(self.client().get('/work-records/jobs/2').status_code,404)

    def test_technician_cannot_spoof_staff_id(self):
        client=self.client('technician')
        r=client.post('/work-records/jobs/1/start',data={'csrf':'token','staff_id':'2','request_key':'timer-request-key-1'})
        self.assertEqual(r.status_code,303)
        self.assertEqual(self.repo.detail(1,1)['labor'][0]['staff_id'],1)

    def test_vehicle_page_rejects_other_tenant(self):
        self.assertEqual(self.client().get('/work-records/vehicles/2').status_code,404)
        self.assertEqual(self.client('technician').get('/work-records/vehicles/1').status_code,403)


if __name__=='__main__':unittest.main()
