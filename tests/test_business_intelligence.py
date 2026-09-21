import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch, Mock
from flask import Flask

from src.config import Config
from src.db_manager import init_db, close_db
from src.migrate_intelligence import migrate
from src.services.intelligence.facts import IntelligenceFacts
from src.services.intelligence.events import EventQueue
from src.services.orchestrators.business_intelligence import BusinessIntelligence
from src.services.llm.base import NullProvider, RecordingProvider
from src.services.agents.base import Finding, AgentResult
from src.services.repositories.agent_repository import AgentRepository


class IntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name,'test.db')
        self.config = patch.object(Config,'DB_PATH',self.path)
        self.config.start()
        init_db()
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executemany("INSERT INTO studios(id,name,city,owner,logo,username,password) VALUES(?,?, 'City','Owner','','user'||?,'x')",
                              [(1,'One',1),(2,'Two',2)])
        self.conn.commit()
        self.repo = IntelligenceFacts(connection=self.conn)

    def tearDown(self):
        self.conn.close()
        self.config.stop()
        self.temp.cleanup()

    def insert(self,table,**values):
        columns=','.join(values)
        result=self.conn.execute(f'INSERT INTO {table}({columns}) VALUES({",".join("?" for _ in values)})',tuple(values.values()))
        self.conn.commit()
        return result.lastrowid

    def job(self,**values):
        fields=dict(studio_id=1,car='Vehicle',service='Detail',status='Completed',price=100,completed_at='2026-09-20')
        fields.update(values)
        return self.insert('jobs',**fields)

    def snapshot(self):
        return self.repo.snapshot(1,30,today=date(2026,9,20))

    def facts(self):
        return {f['id']:f for f in self.snapshot()['facts']}

    def answer(self,provider=None,question='What are business priorities?'):
        return BusinessIntelligence(self.repo,provider or NullProvider()).answer(1,question,'owner')

    def test_money_conversion_and_adjacent_windows(self):
        self.job(price=125,completed_at='2026-09-20')
        self.job(price=75,completed_at='2026-08-22')
        self.job(price=100,completed_at='2026-08-21')
        self.job(price=9000,completed_at='2026-09-21')
        self.job(studio_id=2,price=9000)
        facts=self.facts()
        self.assertEqual(facts['job_value']['value'],20000)
        self.assertEqual(facts['value_change']['value'],100)
        self.assertEqual(facts['average_ticket']['value'],10000)

    def test_empty_data_is_not_a_zero_percent_forecast(self):
        facts=self.facts()
        self.assertIsNone(facts['value_change']['value'])
        self.assertIsNone(facts['average_ticket']['value'])
        self.assertIsNone(facts['lead_conversion']['value'])

    def test_estimate_totals_are_already_cents(self):
        self.insert('estimates',vehicle='V',studio_id=1,customer_name='Secret',customer_email='private@example.com',customer_phone='555',status='Sent',total=12500,created_at='2026-09-01')
        self.insert('estimates',vehicle='V',studio_id=2,customer_name='Other',customer_email='',customer_phone='',status='Sent',total=90000)
        self.assertEqual(self.facts()['estimate_pipeline']['value'],12500)
        self.assertEqual(self.facts()['stale_estimates']['value'],1)

    def test_backfilled_leads_excluded_from_cohort(self):
        for source,status in [('manual','won'),('manual','new'),('booking_form','won')]:
            self.insert('leads',studio_id=1,name='Lead',source=source,status=status,created_at='2026-09-15')
        self.assertEqual(self.facts()['lead_conversion']['value'],50)

    def test_rebooking_requires_customer_link_and_excludes_future_booking(self):
        customer=self.insert('customers',studio_id=1,name='Private Name',email='private@example.com',phone='999')
        self.job(customer_id=customer,completed_at='2026-01-01')
        self.job(completed_at='2026-01-01')
        self.assertEqual(self.facts()['rebooking']['value'],1)
        self.insert('bookings',service_id=1,bay_id=1,staff_id=1,studio_id=1,customer_id=customer,customer_name='Private Name',customer_phone='999',vehicle='V',date='2026-09-22',time_slot='10:00',status='Confirmed')
        self.assertEqual(self.facts()['rebooking']['value'],0)

    def test_customer_join_cannot_cross_tenants(self):
        customer=self.insert('customers',studio_id=2,name='Other')
        self.job(customer_id=customer,completed_at='2026-01-01')
        self.assertEqual(self.facts()['rebooking']['value'],0)

    def test_sparse_stock_data_does_not_invent_coverage(self):
        item=self.insert('inventory_items',studio_id=1,sku='A',name='Coating',quantity=3,reorder_level=5)
        self.insert('inventory_logs',studio_id=1,item_id=item,change_qty=-30,reason='Use',created_at='2026-09-19')
        self.assertNotIn('stock_'+str(item),self.facts())
        self.assertEqual(self.facts()['low_stock']['value'],1)

    def test_stock_coverage_uses_withdrawals_and_tenant_safe_join(self):
        item=self.insert('inventory_items',studio_id=1,sku='A',name='Coating',quantity=3,reorder_level=1)
        for day in (17,18,19):
            self.insert('inventory_logs',studio_id=1,item_id=item,change_qty=-10,reason='Use',created_at=f'2026-09-{day}')
        self.insert('inventory_logs',studio_id=2,item_id=item,change_qty=-3000,reason='Bad link',created_at='2026-09-19')
        self.assertEqual(self.facts()['stock_'+str(item)]['value'],3)

    def test_valid_model_selection_uses_only_server_claims(self):
        provider=RecordingProvider([json.dumps(dict(supported=True,fact_ids=['job_value'],action_ids=[], invented_revenue=999999))])
        result=self.answer(provider,'How is revenue?')
        self.assertEqual(result['source'],'ai')
        self.assertEqual(result['fact_ids'],['job_value'])
        self.assertNotIn('999999',json.dumps(result))

    def test_hallucinated_ids_fail_closed_to_verified_fallback(self):
        provider=RecordingProvider(['{"supported":true,"fact_ids":["tenant_2_secret"],"action_ids":[]}'])
        result=self.answer(provider)
        self.assertEqual(result['source'],'deterministic')
        self.assertNotIn('tenant_2_secret',json.dumps(result))

    def test_selected_action_always_attaches_its_evidence(self):
        self.job(status='Pending')
        provider=RecordingProvider(['{"supported":true,"fact_ids":["job_value"],"action_ids":["assign_work"]}'])
        result=self.answer(provider)
        self.assertEqual(result['source'],'ai')
        self.assertIn('unassigned',result['fact_ids'])

    def test_targeted_question_discards_unrelated_model_actions(self):
        self.job(status='Pending')
        provider=RecordingProvider(['{"supported":true,"fact_ids":["job_value"],"action_ids":["assign_work"]}'])
        result=self.answer(provider,question='What is completed revenue?')
        self.assertEqual(result['source'],'ai')
        self.assertEqual(result['action_ids'],[])

    def test_malformed_model_output_falls_back_and_saves(self):
        result=self.answer(RecordingProvider(['not json']))
        self.assertEqual(result['source'],'deterministic')
        self.assertGreater(result['report_id'],0)

    def test_unsupported_model_answer_does_not_guess(self):
        result=self.answer(RecordingProvider(['{"supported":false,"fact_ids":[],"action_ids":[]}']),'What is actual profit?')
        self.assertFalse(result['supported'])
        self.assertIn('cannot answer',result['message'])

    def test_invalid_question_rejected_before_model_call(self):
        provider=RecordingProvider()
        with self.assertRaises(ValueError):
            self.answer(provider,question=[])
        self.assertFalse(provider.calls)

    def test_prompt_omits_customer_pii_and_other_studio_data(self):
        self.insert('customers',studio_id=1,name='Hidden Customer',email='private@example.com',phone='999')
        self.job(studio_id=2,service='OTHER_STUDIO_SECRET')
        provider=RecordingProvider(['{"supported":true,"fact_ids":["job_value"],"action_ids":[]}'])
        self.answer(provider)
        prompt=provider.calls[0]['prompt']
        for value in ('Hidden Customer','private@example.com','OTHER_STUDIO_SECRET'):
            self.assertNotIn(value,prompt)

    def test_saved_report_is_frozen_and_tenant_scoped(self):
        service=BusinessIntelligence(self.repo,NullProvider())
        result=service.answer(1,'Business briefing','owner')
        self.job(price=900)
        saved=service.report(1,result['report_id'])
        self.assertEqual(saved['snapshot'],result['snapshot'])
        self.assertIsNone(service.report(2,result['report_id']))

    def test_events_are_atomic_and_coalesced(self):
        self.conn.execute("INSERT INTO jobs(studio_id,car,service) VALUES(1,'V','Detail')")
        self.conn.rollback()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM intelligence_events').fetchone()[0],0)
        self.job();self.job()
        rows=self.conn.execute('SELECT * FROM intelligence_events').fetchall()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['revision'],2)

    def test_new_event_during_processing_is_not_lost(self):
        self.job()
        queue=EventQueue(connection=self.conn)
        event=queue.claim()
        self.assertIsNone(queue.claim())
        self.job()
        queue.finish(event)
        self.assertIsNotNone(queue.claim())

    def test_failed_event_retries_and_keeps_error(self):
        self.job()
        queue=EventQueue(connection=self.conn)
        event=queue.claim();queue.finish(event,'test failure')
        self.assertIsNone(queue.claim())
        pending=queue.status(1)[0]
        self.assertEqual(pending['attempts'],1)
        self.assertEqual(pending['last_error'],'test failure')

    def test_exhausted_events_only_retry_in_own_studio(self):
        self.job();self.job(studio_id=2)
        self.conn.execute('UPDATE intelligence_events SET attempts=5');self.conn.commit()
        queue=EventQueue(connection=self.conn)
        self.assertIsNone(queue.claim())
        self.assertEqual(queue.retry_failed(1),1)
        self.assertEqual(queue.claim()['studio_id'],1)
        self.assertEqual(queue.status(2)[0]['attempts'],5)

    def test_expired_event_lease_recovers(self):
        self.job();queue=EventQueue(connection=self.conn);first=queue.claim()
        self.conn.execute('UPDATE intelligence_events SET lease_until=0');self.conn.commit()
        second=queue.claim()
        self.assertNotEqual(first['token'],second['token'])
        queue.finish(first)
        self.assertEqual(queue.status(1)[0]['processed_revision'],0)

    def test_disabled_agent_skipped_when_event_drained(self):
        self.job();queue=EventQueue(connection=self.conn)
        runner=Mock();runner.repo.settings.return_value={'business':{'enabled':False}}
        runner.run_agent.return_value=AgentResult(agent='diagnosis',findings=[])
        self.assertEqual(queue.drain(runner),1)
        self.assertEqual(runner.run_agent.call_count,1)
        self.assertEqual(runner.run_agent.call_args.args[1],'diagnosis')

    def test_lead_score_updates_do_not_trigger_infinite_loop(self):
        lead=self.insert('leads',studio_id=1,name='Lead')
        before=self.conn.execute("SELECT revision FROM intelligence_events WHERE event='leads_changed'").fetchone()[0]
        self.conn.execute('UPDATE leads SET score=50,score_reason=? WHERE id=?',('Rule',lead));self.conn.commit()
        after=self.conn.execute("SELECT revision FROM intelligence_events WHERE event='leads_changed'").fetchone()[0]
        self.assertEqual(before,after)

    def test_migration_repeat_does_not_reset_events(self):
        self.job();migrate(self.path)
        self.assertEqual(len(EventQueue(connection=self.conn).status(1)),1)

    def test_resolved_finding_reopens_and_json_is_not_truncated(self):
        repo=AgentRepository(connection=self.conn)
        finding=Finding(kind='test',title='Issue',data={'evidence':'x'*5000})
        repo.save_findings(1,'business',None,[finding]);repo.resolve_missing(1,'business',[])
        repo.save_findings(1,'business',None,[finding])
        row=repo.open_findings(1)[0]
        self.assertEqual(len(json.loads(row['data_json'])['evidence']),5000)

    def test_dismissed_finding_does_not_reopen_on_lower_severity(self):
        repo=AgentRepository(connection=self.conn)
        finding=Finding(kind='test',title='Issue',severity='critical')
        repo.save_findings(1,'business',None,[finding]);row=repo.open_findings(1)[0]
        repo.set_status(1,row['id'],'dismissed','owner');finding.severity='warning'
        repo.save_findings(1,'business',None,[finding])
        self.assertFalse(repo.open_findings(1))

    def test_update_result_not_previous_insert_id(self):
        repo=AgentRepository(connection=self.conn)
        self.job()
        self.assertFalse(repo.set_status(1,999,'dismissed','owner'))

    def app(self):
        from src.routes.intelligence_routes import bp
        app=Flask(__name__,template_folder=os.path.abspath('templates'))
        app.secret_key='test-only';app.config['TESTING']=True
        app.register_blueprint(bp);app.teardown_appcontext(close_db)
        return app

    def client(self,role='admin',studio=1):
        client=self.app().test_client()
        with client.session_transaction() as session:
            session.update(logged_in=True,role=role,studio_id=studio,user_id=1,intelligence_csrf='token')
        return client

    def test_owner_access_and_technician_denial(self):
        self.assertEqual(self.client().get('/intelligence/api/snapshot').status_code,200)
        self.assertEqual(self.client('technician').get('/intelligence/api/snapshot').status_code,403)

    def test_csrf_and_invalid_payload(self):
        client=self.client()
        self.assertEqual(client.post('/intelligence/api/ask',json={'question':'Brief me'}).status_code,403)
        headers={'X-CSRF-Token':'token'}
        for data in ([],{'question':'x'},{'question':'Brief me','days':'bad'}):
            self.assertEqual(client.post('/intelligence/api/ask',json=data,headers=headers).status_code,400)

    def test_report_route_denies_other_studio(self):
        report=self.answer()
        self.assertEqual(self.client(studio=2).get('/intelligence/api/reports/'+str(report['report_id'])).status_code,404)

    def test_period_validation(self):
        for value in ('0','100000','x','1.2'):
            self.assertEqual(self.client().get('/intelligence/api/snapshot?days='+value).status_code,400)

    def test_agent_lease_prevents_duplicate_run(self):
        import time
        from src.services.agents.runtime import AgentRunner
        repo=AgentRepository(connection=self.conn)
        self.conn.execute('INSERT INTO agent_leases VALUES(1,?,?,?)',('business','other-worker',time.time()+60))
        self.conn.commit()
        result=AgentRunner(repo).run_agent(1,'business')
        self.assertFalse(result.ok)
        self.assertIn('already running',result.error)
        self.assertEqual(repo.recent_runs(1),[])

    def test_statement_error_preserved_in_flask_context(self):
        from src.services.repositories.base import BaseRepository
        with self.app().app_context():
            with self.assertRaises(sqlite3.OperationalError):
                BaseRepository().execute('INSERT INTO does_not_exist VALUES(1)')


if __name__=='__main__':
    unittest.main()
