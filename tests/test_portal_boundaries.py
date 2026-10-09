"""Pre-push regression checks for the portal/security work in progress."""
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from flask import Flask
import test_job_records as fixtures
from src.config import Config
from src.db_manager import close_db
from src.services.repositories.portal import Portal
from src.services.repositories.job_records import RecordError
from src.security import install_security,safe_next


class PortalBoundaryTests(unittest.TestCase):
    setUp=fixtures.JobRecordTests.setUp
    tearDown=fixtures.JobRecordTests.tearDown

    def app(self):
        from src.routes.portal_routes import bp,admin_bp
        from src.controller import bp as main
        app=Flask(__name__,template_folder=os.path.abspath('templates'),static_folder=Config.STATIC_FOLDER)
        app.secret_key='test';app.config['TESTING']=True
        app.register_blueprint(main);app.register_blueprint(bp);app.register_blueprint(admin_bp)
        app.teardown_appcontext(close_db);install_security(app)
        return app

    def client(self,role='customer',cid=1,sid=1):
        client=self.app().test_client()
        with client.session_transaction() as s:s.update(logged_in=True,role=role,user_id=cid,studio_id=sid,work_records_csrf='token')
        return client

    def test_customer_cannot_open_staff_pages(self):
        client=self.client()
        for url in ('/dashboard','/jobs','/media','/portal-admin/'):
            self.assertEqual(client.get(url).status_code,403,url)
        self.assertEqual(client.get('/portal/').status_code,200)
        self.assertEqual(client.get('/portal/estimates/999').status_code,404)

    def test_portal_entry_routes_managers_without_exposing_customer_records(self):
        for role in ('admin','general_manager'):
            response=self.client(role=role).get('/portal/')
            self.assertEqual(response.status_code,302)
            self.assertEqual(response.location,'/portal-admin/')
        self.assertEqual(self.client(role='technician').get('/portal/').status_code,403)
        response=self.app().test_client().get('/portal/')
        self.assertEqual(response.status_code,302)
        self.assertIn('/login?next=',response.location)

    def test_crm_invitation_link_is_complete_and_customer_selection_is_scoped(self):
        client=self.client(role='admin')
        page=client.get('/portal-admin/?customer_id=1')
        self.assertEqual(page.status_code,200)
        self.assertIn(b'value="1" selected',page.data)
        page=client.post('/portal-admin/',data={'csrf':'token','action':'invite','customer_id':'1'})
        self.assertEqual(page.status_code,200)
        self.assertIn(b'http://localhost/portal/activate/',page.data)
        page=client.post('/portal-admin/',data={'csrf':'token','action':'invite','customer_id':'2'})
        self.assertNotIn(b'id="inviteLink"',page.data)

    def test_records_require_current_vehicle_owner(self):
        self.repo.link_vehicle(1,1,1,'owner');repo=Portal(connection=self.conn)
        self.assertEqual(len(repo.records(1,1)['jobs']),1)
        self.conn.execute('UPDATE vehicles SET customer_id=2 WHERE id=1');self.conn.commit()
        self.assertEqual(repo.records(1,1)['jobs'],[])

    def test_invitation_is_single_use_and_cannot_reset_existing_account(self):
        repo=Portal(connection=self.conn);token=repo.invite(1,1,'owner')
        repo.accept_invite(token,'portal-test-user','Test-Password1!')
        with self.assertRaises(RecordError):repo.accept_invite(token,'another-user','Test-Password1!')
        with self.assertRaises(RecordError):repo.invite(1,1,'owner')
        customer=self.conn.execute('SELECT password FROM customers WHERE id=1').fetchone()[0]
        self.assertNotEqual(customer,'Test-Password1!')

    def test_revoked_and_expired_invites_are_rejected(self):
        repo=Portal(connection=self.conn);token=repo.invite(1,1,'owner',now=100)
        with self.assertRaises(RecordError):repo.accept_invite(token,'portal-user','Test-Password1!',now=200000)
        repo.revoke_invites(1,1,'owner')
        with self.assertRaises(RecordError):repo.accept_invite(token,'portal-user','Test-Password1!',now=101)

    def test_private_media_blocks_anonymous_other_customer_and_traversal(self):
        self.repo.link_vehicle(1,1,1,'owner')
        self.conn.execute("INSERT INTO customers(id,studio_id,name) VALUES(3,1,'Other customer')")
        self.conn.execute("INSERT INTO media(studio_id,job_id,stage,filename,original_name,media_type,customer_visible) VALUES(1,1,'after','test.png','test.png','image',1)");self.conn.commit()
        private=Path(self.temp.name)/'private';folder=private/'studio_1';folder.mkdir(parents=True);(folder/'test.png').write_bytes(b'fixture')
        with patch.object(Config,'PRIVATE_UPLOAD_ROOT',str(private)):
            self.assertEqual(self.client().get('/uploads/studio_1/test.png').status_code,200)
            self.assertEqual(self.client(cid=3).get('/uploads/studio_1/test.png').status_code,404)
            self.assertEqual(self.app().test_client().get('/uploads/studio_1/test.png').status_code,404)
            self.assertEqual(self.client(cid=3).get('/static/assets/../uploads/studio_1/test.png').status_code,404)
        self.conn.execute('UPDATE media SET customer_visible=0');self.conn.commit()
        self.assertEqual(self.client().get('/uploads/studio_1/test.png').status_code,404)

    def test_legacy_tracking_is_retired(self):
        self.assertEqual(self.app().test_client().get('/customer/tracking/known-token').status_code,410)

    def test_production_rejects_missing_secret_and_unsafe_redirects(self):
        with patch.object(Config,'PRODUCTION',True),patch.dict(os.environ,{'SECRET_KEY':''}):
            with self.assertRaises(RuntimeError):Config.validate()
        for url in ('https://example.test','//example.test','/\\example.test','/\nexample'):
            self.assertEqual(safe_next(url),'/dashboard')
        self.assertEqual(safe_next('/jobs?filter=active'),'/jobs?filter=active')

    def test_warranty_claim_is_owned_and_idempotent(self):
        self.repo.link_vehicle(1,1,1,'owner');self.conn.execute("UPDATE jobs SET status='Completed',completed_at='2026-01-01' WHERE id=1");self.conn.commit()
        repo=Portal(connection=self.conn)
        terms=dict(title='Demo warranty',coverage='Demo coverage',exclusions='Demo exclusions',care='Demo care',starts_on='2026-01-01',ends_on='2027-01-01',eligibility_note='Internal eligibility evidence')
        wid=repo.issue_warranty(1,1,terms,'owner','warranty-request-key-1')
        claim=repo.claim(1,1,wid,'Demo issue','claim-request-key-1')
        self.assertEqual(repo.claim(1,1,wid,'Demo issue','claim-request-key-1'),claim)
        with self.assertRaises(RecordError):repo.claim(1,2,wid,'Demo issue','claim-request-key-2')
        repo.resolve_claim(1,claim,'Under review','We will inspect',1,'owner')
        with self.assertRaises(RecordError):repo.resolve_claim(1,claim,'Accepted','Approved',1,'owner')
        page=self.client().get('/portal/')
        self.assertNotIn(b'Internal eligibility evidence',page.data)
