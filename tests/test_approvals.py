import hashlib
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
import test_vehicle_workflow as fixtures
from src.services.repositories.approvals import Approvals
from src.services.repositories.job_records import RecordError


class ApprovalTests(unittest.TestCase):
    setUp=fixtures.VehicleWorkflowTests.setUp
    tearDown=fixtures.VehicleWorkflowTests.tearDown
    form=fixtures.VehicleWorkflowTests.form

    def prepare(self):
        self.eid=fixtures.VehicleWorkflowTests.prepare(self)
        self.conn.execute("UPDATE estimates SET status='Draft',tax_amount=1253,tax_percent=10,internal_notes='private-business-note' WHERE id=?",(self.eid,))
        self.conn.execute("INSERT INTO estimate_items(estimate_id,name,description,quantity,unit_price,total) VALUES(?,'Detail','Care package',1,12525,12525)",(self.eid,))
        self.conn.commit();self.approvals=Approvals(connection=self.conn)
        return self.approvals.issue(1,self.eid,'owner')

    def test_issue_hashes_token_and_freezes_customer_only_snapshot(self):
        token,version=self.prepare();data=self.approvals.read(token)
        self.assertEqual(data['est']['total'],13778)
        self.assertNotIn('private-business-note',data['version']['snapshot'])
        self.assertEqual(data['version']['token_hash'],hashlib.sha256(token.encode()).hexdigest())
        self.assertNotIn(token,str(dict(self.conn.execute('SELECT * FROM estimate_approval_versions').fetchone())))

    def test_expiry_revocation_and_new_link_invalidation(self):
        token,version=self.prepare()
        with self.assertRaises(RecordError):self.approvals.read(token,now=int(time.time())+8*86400)
        new,new_version=self.approvals.issue(1,self.eid,'owner')
        with self.assertRaises(RecordError):self.approvals.read(token)
        self.approvals.revoke(1,self.eid,new_version,'owner')
        with self.assertRaises(RecordError):self.approvals.read(new)

    def test_changed_quote_cannot_be_approved(self):
        token,_=self.prepare()
        self.conn.execute("UPDATE estimates SET total=999 WHERE id=?",(self.eid,));self.conn.commit()
        with self.assertRaises(RecordError):self.approvals.decide(token,'approve','Customer','yes')
        self.assertEqual(self.conn.execute('SELECT status FROM estimates WHERE id=?',(self.eid,)).fetchone()[0],'Sent')

    def test_approval_replay_is_idempotent_but_not_reversible(self):
        token,_=self.prepare()
        self.approvals.decide(token,'approve','Customer','yes')
        self.assertEqual(self.approvals.decide(token,'approve','Customer','yes'),'approve')
        with self.assertRaises(RecordError):self.approvals.decide(token,'revision','Customer',None)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM job_record_audit WHERE action='customer_quote_response'").fetchone()[0],1)
        with self.assertRaises(RecordError):self.approvals.issue(1,self.eid,'owner')

    def test_changed_approved_version_cannot_book(self):
        token,_=self.prepare();self.approvals.decide(token,'approve','Customer','yes')
        self.conn.execute('UPDATE estimates SET subtotal=20000 WHERE id=?',(self.eid,));self.conn.commit()
        with self.assertRaises(RecordError):self.workflow.book(1,self.form(estimate_id=self.eid),'owner')

    def test_revision_response_leaves_estimate_unapproved(self):
        token,_=self.prepare();self.approvals.decide(token,'revision','Customer',None)
        self.assertEqual(self.approvals.read(token)['version']['decision'],'revision')
        self.assertEqual(self.conn.execute('SELECT status FROM estimates WHERE id=?',(self.eid,)).fetchone()[0],'Revision Requested')

    def test_conflicting_concurrent_decisions_only_one_wins(self):
        token,_=self.prepare()
        def decide(action):
            try:return Approvals(db_path=self.path).decide(token,action,'Customer','yes')
            except RecordError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(decide,('approve','revision')))
        self.assertEqual(sum(r is not None for r in result),1)

    def test_wrong_studio_and_missing_acknowledgment_rejected(self):
        token,version=self.prepare()
        with self.assertRaises(RecordError):self.approvals.issue(2,self.eid,'foreign')
        with self.assertRaises(RecordError):self.approvals.revoke(2,self.eid,version,'foreign')
        with self.assertRaises(RecordError):self.approvals.decide(token,'approve','Customer',None)
        with self.assertRaises(RecordError):self.approvals.decide(token,'approve','x'*121,'yes')

    def test_public_routes_retire_numeric_access_and_guard_csrf(self):
        token,_=self.prepare()
        app=fixtures.VehicleWorkflowTests.app(self)
        from src.controller import bp as main
        from src.routes.approval_routes import bp
        app.register_blueprint(main);app.register_blueprint(bp);client=app.test_client()
        self.assertEqual(client.get(f'/estimates/{self.eid}/approve').status_code,410)
        self.assertEqual(client.post(f'/estimates/{self.eid}/approve',data={'signature':'Guess','action':'approve'}).status_code,410)
        response=client.get('/approvals/'+token)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertEqual(response.headers['Referrer-Policy'],'no-referrer')
        self.assertNotIn(b'private-business-note',response.data)
        self.assertEqual(client.post('/approvals/'+token,data={'action':'approve','signer':'Customer','confirmed':'yes'}).status_code,403)
        with client.session_transaction() as session:csrf=session['approval_csrf']
        self.assertEqual(client.post('/approvals/'+token,data={'csrf':csrf,'action':'approve','signer':'Customer','confirmed':'yes'}).status_code,303)

    def test_management_requires_manager_csrf_and_matching_tenant(self):
        self.prepare();app=fixtures.VehicleWorkflowTests.app(self)
        from src.routes.approval_routes import bp
        app.register_blueprint(bp);client=app.test_client()
        with client.session_transaction() as s:s.update(logged_in=True,studio_id=1,role='technician',staff_id=1,work_records_csrf='token')
        self.assertEqual(client.get(f'/approvals/manage/{self.eid}').status_code,403)
        with client.session_transaction() as s:s['role']='admin'
        self.assertEqual(client.get(f'/approvals/manage/{self.eid}').status_code,200)
        self.assertEqual(client.post(f'/approvals/manage/{self.eid}',data={'action':'issue'}).status_code,403)
        self.assertEqual(client.post(f'/approvals/manage/{self.eid}',data={'action':'issue','csrf':'token'}).status_code,200)
        with client.session_transaction() as s:s['studio_id']=2
        self.assertEqual(client.get(f'/approvals/manage/{self.eid}').status_code,404)

    def test_logging_redacts_bearer_tokens(self):
        import logging
        from src.logging_config import ApprovalTokenFilter
        token,_=self.prepare()
        record=logging.LogRecord('werkzeug',20,'',0,'GET %s HTTP/1.1',('/approvals/'+token,),None)
        ApprovalTokenFilter().filter(record)
        self.assertNotIn(token,record.getMessage());self.assertIn('[redacted]',record.getMessage())

    def test_inconsistent_line_price_cannot_be_shared(self):
        self.prepare();self.conn.execute('UPDATE estimate_items SET quantity=2 WHERE estimate_id=?',(self.eid,));self.conn.commit()
        with self.assertRaises(RecordError):self.approvals.issue(1,self.eid,'owner')
