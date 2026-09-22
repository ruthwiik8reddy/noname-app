"""Atomic job records and direct-cost accounting; all money is integer cents."""
import json
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from .base import BaseRepository


class RecordError(ValueError):
    pass


def number(value, scale=1):
    try:
        n=Decimal(str(value))*scale
        if not n.is_finite() or n<0 or n>1_000_000_000 or n!=n.to_integral_value():
            raise ValueError()
        return int(n)
    except (ValueError,InvalidOperation,TypeError):
        raise RecordError('Enter a non-negative amount with valid precision.')


def rounded(value):
    return int(value.quantize(Decimal('1'),rounding=ROUND_HALF_UP))


class JobRecords(BaseRepository):
    def _job(self,conn,sid,jid):
        row=conn.execute('SELECT * FROM jobs WHERE studio_id=? AND id=?',(sid,jid)).fetchone()
        if not row:
            raise RecordError('Job not found.')
        return dict(row)

    def _audit(self,conn,sid,jid,action,actor,detail):
        conn.execute('INSERT INTO job_record_audit(studio_id,job_id,action,actor,detail) VALUES(?,?,?,?,?)',
                     (sid,jid,action,str(actor),json.dumps(detail)))

    def link_vehicle(self,sid,jid,vid,actor):
        with self._conn() as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            job=self._job(conn,sid,jid)
            if job.get('booking_id'):
                source=conn.execute('SELECT vehicle_id FROM bookings WHERE studio_id=? AND id=?',(sid,job['booking_id'])).fetchone()
                if not source or source['vehicle_id']!=vid:
                    raise RecordError('This job must retain the vehicle from its source booking.')
            vehicle=conn.execute('''SELECT v.* FROM vehicles v JOIN customers c ON c.id=v.customer_id AND c.studio_id=v.studio_id
                                   WHERE v.studio_id=? AND v.id=?''',(sid,vid)).fetchone()
            if not vehicle:
                raise RecordError('Vehicle not found in this studio.')
            if job['customer_id'] is not None and job['customer_id']!=vehicle['customer_id']:
                raise RecordError('The vehicle belongs to a different customer. Correct the customer record first.')
            conn.execute('UPDATE jobs SET vehicle_id=?,customer_id=? WHERE id=? AND studio_id=?',
                         (vid,vehicle['customer_id'],jid,sid))
            self._audit(conn,sid,jid,'vehicle_linked',actor,{'previous_vehicle_id':job['vehicle_id'],'vehicle_id':vid})

    def set_rate(self,sid,staff_id,hourly,actor):
        rate=number(hourly,100)
        with self._conn() as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute('SELECT id FROM staff WHERE studio_id=? AND id=?',(sid,staff_id)).fetchone():
                raise RecordError('Staff member not found.')
            conn.execute('''INSERT INTO staff_cost_rates(studio_id,staff_id,hourly_cents) VALUES(?,?,?)
                          ON CONFLICT(studio_id,staff_id) DO UPDATE SET hourly_cents=excluded.hourly_cents,updated_at=datetime('now')''',(sid,staff_id,rate))
            self._audit(conn,sid,None,'labor_rate',actor,{'staff_id':staff_id,'hourly_cents':rate})

    def plan(self,sid,jid,minutes,materials,actor):
        minutes=number(minutes); materials=number(materials,100)
        with self._conn() as conn,conn:
            self._job(conn,sid,jid)
            conn.execute('''INSERT INTO job_cost_plans(studio_id,job_id,planned_minutes,planned_material_cents) VALUES(?,?,?,?)
                ON CONFLICT(studio_id,job_id) DO UPDATE SET planned_minutes=excluded.planned_minutes,
                planned_material_cents=excluded.planned_material_cents,reviewed_at=NULL,reviewed_by=NULL''',(sid,jid,minutes,materials))
            self._audit(conn,sid,jid,'cost_plan',actor,{'minutes':minutes,'materials_cents':materials})

    def _key(self,key):
        if not isinstance(key,str) or not 16<=len(key)<=100:
            raise RecordError('A valid request key is required. Reload the page and try again.')

    def start(self,sid,jid,staff_id,actor,key,now=None):
        self._key(key); now=int(time.time() if now is None else now)
        with self._conn() as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            job=self._job(conn,sid,jid)
            repeat=conn.execute('SELECT * FROM job_labor WHERE studio_id=? AND request_key=?',(sid,key)).fetchone()
            if repeat:
                if repeat['job_id']!=jid or repeat['staff_id']!=staff_id:
                    raise RecordError('Request key already used for another timer.')
                return repeat['id']
            if job['status'].lower() not in ('pending','in progress'):
                raise RecordError('Timers can only start on pending or in-progress jobs.')
            if not conn.execute('SELECT id FROM staff WHERE studio_id=? AND id=?',(sid,staff_id)).fetchone():
                raise RecordError('Staff member not found.')
            if conn.execute('SELECT id FROM job_labor WHERE studio_id=? AND staff_id=? AND ended_at IS NULL',(sid,staff_id)).fetchone():
                raise RecordError('This staff member already has a running timer. Stop it first.')
            rate=conn.execute('SELECT hourly_cents FROM staff_cost_rates WHERE studio_id=? AND staff_id=?',(sid,staff_id)).fetchone()
            entry=conn.execute('INSERT INTO job_labor(studio_id,job_id,staff_id,started_at,hourly_cents,actor,request_key) VALUES(?,?,?,?,?,?,?)',
                              (sid,jid,staff_id,now,rate[0] if rate else None,str(actor),key)).lastrowid
            self._audit(conn,sid,jid,'timer_started',actor,{'timer_id':entry,'staff_id':staff_id})
            return entry

    def stop(self,sid,jid,timer_id,actor,staff_id=None,now=None):
        now=int(time.time() if now is None else now)
        with self._conn() as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            timer=conn.execute('SELECT * FROM job_labor WHERE studio_id=? AND job_id=? AND id=?',(sid,jid,timer_id)).fetchone()
            if not timer or (staff_id is not None and timer['staff_id']!=staff_id):
                raise RecordError('Timer not found or belongs to another staff member.')
            if timer['ended_at'] is None:
                conn.execute('UPDATE job_labor SET ended_at=? WHERE id=?',(max(now,timer['started_at']),timer_id))
                self._audit(conn,sid,jid,'timer_stopped',actor,{'timer_id':timer_id})

    def material(self,sid,jid,item_id,qty,actor,key,reason='Used on job',return_of=None):
        self._key(key); units=number(qty,1000)
        if units<=0 or not isinstance(reason,str) or not reason.strip() or len(reason)>500:
            raise RecordError('A positive quantity (up to three decimal places) and a reason are required.')
        with self._conn() as conn,conn:
            conn.execute('BEGIN IMMEDIATE'); self._job(conn,sid,jid)
            repeat=conn.execute('SELECT * FROM job_material_entries WHERE studio_id=? AND request_key=?',(sid,key)).fetchone()
            signed=-units if return_of else units
            if repeat:
                if (repeat['job_id'],repeat['item_id'],repeat['quantity_milli'],repeat['return_of'])!=(jid,item_id,signed,return_of):
                    raise RecordError('Request key already used for a different stock movement.')
                return repeat['id']
            item=conn.execute('SELECT * FROM inventory_items WHERE studio_id=? AND id=?',(sid,item_id)).fetchone()
            if not item:
                raise RecordError('Inventory item not found.')
            rate=int(item['cost_per_unit']) if item['cost_per_unit']>0 else None
            cost=rounded(Decimal(units)*rate/1000) if rate is not None else None
            name,unit=item['name'],item['unit']
            if return_of:
                original=conn.execute('SELECT * FROM job_material_entries WHERE studio_id=? AND job_id=? AND id=? AND item_id=? AND return_of IS NULL',
                                      (sid,jid,return_of,item_id)).fetchone()
                if not original:
                    raise RecordError('Original consumption entry not found.')
                returned=conn.execute('SELECT COALESCE(SUM(quantity_milli),0),COALESCE(SUM(cost_cents),0) FROM job_material_entries WHERE studio_id=? AND return_of=?',(sid,return_of)).fetchone()
                remaining=original['quantity_milli']+returned[0]
                if units>remaining:
                    raise RecordError('Return exceeds the amount consumed on this job.')
                rate=original['unit_cost_cents'];name,unit=original['item_name'],original['unit']
                cost=None if rate is None else (original['cost_cents']+returned[1] if units==remaining else min(max(0,original['cost_cents']+returned[1]),rounded(Decimal(units)*rate/1000)))
                cost=-cost if cost is not None else None
            delta=Decimal(units)/1000 * (1 if return_of else -1)
            stock=Decimal(str(item['quantity']))+delta
            if stock<0:
                raise RecordError('Not enough stock. No material or stock entry was recorded.')
            conn.execute('UPDATE inventory_items SET quantity=?,updated_at=datetime(\'now\') WHERE studio_id=? AND id=?',(float(stock),sid,item_id))
            conn.execute('INSERT INTO inventory_logs(studio_id,item_id,change_qty,reason) VALUES(?,?,?,?)',(sid,item_id,float(delta),f'Job #{jid}: {reason}'))
            entry=conn.execute('''INSERT INTO job_material_entries(studio_id,job_id,item_id,item_name,unit,quantity_milli,
                      unit_cost_cents,cost_cents,return_of,actor,reason,request_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                      (sid,jid,item_id,name,unit,signed,rate,cost,return_of,str(actor),reason,key)).lastrowid
            self._audit(conn,sid,jid,'material_returned' if return_of else 'material_used',actor,{'entry_id':entry})
            return entry

    def correct_cost(self,sid,jid,kind,entry_id,rate,actor,reason):
        rate=number(rate,100)
        if kind not in ('labor','material') or not isinstance(reason,str) or not reason.strip() or len(reason)>500:
            raise RecordError('Select labor or material and explain the correction.')
        table='job_labor' if kind=='labor' else 'job_material_entries'
        with self._conn() as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute(f'SELECT * FROM {table} WHERE studio_id=? AND job_id=? AND id=?',(sid,jid,entry_id)).fetchone()
            if not row or (kind=='material' and row['return_of'] is not None):
                raise RecordError('Original work entry not found.')
            if kind=='labor':
                conn.execute('UPDATE job_labor SET hourly_cents=? WHERE id=?',(rate,entry_id))
            else:
                cost=rounded(Decimal(row['quantity_milli'])*rate/1000)
                conn.execute('UPDATE job_material_entries SET unit_cost_cents=?,cost_cents=? WHERE id=?',(rate,cost,entry_id))
                remaining=row['quantity_milli']
                for returned in conn.execute('SELECT * FROM job_material_entries WHERE studio_id=? AND return_of=? ORDER BY id',(sid,entry_id)).fetchall():
                    units=-returned['quantity_milli']
                    refund=cost if units==remaining else min(cost,rounded(Decimal(units)*rate/1000))
                    conn.execute('UPDATE job_material_entries SET unit_cost_cents=?,cost_cents=? WHERE id=?',(rate,-refund,returned['id']))
                    remaining-=units;cost-=refund
            self._audit(conn,sid,jid,'cost_corrected',actor,{'kind':kind,'entry_id':entry_id,
                'previous_rate':row['hourly_cents'] if kind=='labor' else row['unit_cost_cents'],'new_rate':rate,'reason':reason})

    def detail(self,sid,jid,now=None):
        now=int(time.time() if now is None else now)
        with self._conn() as conn:
            job=self._job(conn,sid,jid)
            labor=[dict(r) for r in conn.execute('''SELECT l.*,s.name AS staff_name FROM job_labor l LEFT JOIN staff s ON s.id=l.staff_id AND s.studio_id=l.studio_id
                      WHERE l.studio_id=? AND l.job_id=? ORDER BY l.id''',(sid,jid))]
            materials=[dict(r) for r in conn.execute('SELECT * FROM job_material_entries WHERE studio_id=? AND job_id=? ORDER BY id',(sid,jid))]
            plan=conn.execute('SELECT * FROM job_cost_plans WHERE studio_id=? AND job_id=?',(sid,jid)).fetchone()
            vehicle=conn.execute('SELECT * FROM vehicles WHERE studio_id=? AND id=?',(sid,job['vehicle_id'])).fetchone()
        for entry in labor:
            entry['seconds']=max(0,(entry['ended_at'] if entry['ended_at'] is not None else now)-entry['started_at'])
            entry['cost_cents']=None if entry['hourly_cents'] is None else rounded(Decimal(entry['seconds'])*entry['hourly_cents']/3600)
        unknown=sum(r['cost_cents'] is None for r in labor+materials)
        running=sum(r['ended_at'] is None for r in labor)
        labor_cost=sum(r['cost_cents'] or 0 for r in labor)
        material_cost=sum(r['cost_cents'] or 0 for r in materials)
        revenue=rounded(Decimal(str(job['price'] or 0))*100)
        complete=bool(plan and plan['reviewed_at'] and not unknown and not running and job['status'].lower()=='completed')
        return dict(job=job,vehicle=dict(vehicle) if vehicle else None,labor=labor,materials=materials,plan=dict(plan) if plan else {},
                    seconds=sum(r['seconds'] for r in labor),labor_cents=labor_cost,material_cents=material_cost,
                    revenue_cents=revenue,contribution_cents=None if unknown else revenue-labor_cost-material_cost,
                    unknown_costs=unknown,running=running,reviewed=complete)

    def review(self,sid,jid,actor):
        with self._conn() as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            detail=JobRecords(connection=conn).detail(sid,jid)
            if detail['job']['status'].lower()!='completed' or detail['unknown_costs'] or detail['running']:
                raise RecordError('Complete the job and resolve running timers or missing costs before reviewing.')
            conn.execute('''INSERT INTO job_cost_plans(studio_id,job_id,reviewed_at,reviewed_by) VALUES(?,?,datetime('now'),?)
                  ON CONFLICT(studio_id,job_id) DO UPDATE SET reviewed_at=datetime('now'),reviewed_by=excluded.reviewed_by''',(sid,jid,str(actor)))
            self._audit(conn,sid,jid,'costs_reviewed',actor,{'labor_cents':detail['labor_cents'],'material_cents':detail['material_cents']})

    def timeline(self,sid,vid):
        vehicle=self.fetch_one('SELECT * FROM vehicles WHERE studio_id=? AND id=?',(sid,vid))
        if not vehicle:
            raise RecordError('Vehicle not found.')
        jobs=self.fetch_all('SELECT id,service,status,completed_at FROM jobs WHERE studio_id=? AND vehicle_id=? ORDER BY id DESC',(sid,vid))
        inspections=self.fetch_all('''SELECT d.id,d.job_id,d.status,d.created_at,d.summary FROM dvi_inspections d
          JOIN jobs j ON j.id=d.job_id AND j.studio_id=d.studio_id WHERE j.studio_id=? AND j.vehicle_id=? ORDER BY d.id DESC''',(sid,vid))
        return dict(vehicle=vehicle,jobs=jobs,inspections=inspections,
                    estimates=self.fetch_all('SELECT id,status,created_at FROM estimates WHERE studio_id=? AND vehicle_id=? ORDER BY id DESC',(sid,vid)),
                    bookings=self.fetch_all('SELECT id,status,date FROM bookings WHERE studio_id=? AND vehicle_id=? ORDER BY id DESC',(sid,vid)))
