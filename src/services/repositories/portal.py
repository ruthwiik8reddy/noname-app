"""Customer-owned records and explicitly issued warranty terms."""
import hashlib
import json
import secrets
import time
from datetime import date
from .followups import Followups,bounded
from .job_records import RecordError,number
from ...auth import hash_password,validate_password_strength


class Portal(Followups):
    def audit(self,conn,sid,cid,action,actor,detail):
        conn.execute('INSERT INTO portal_audit(studio_id,customer_id,action,actor,detail) VALUES(?,?,?,?,?)',(sid,cid,action,str(actor),json.dumps(detail)))

    def invite(self,sid,cid,actor,now=None):
        now=int(time.time() if now is None else now);token=secrets.token_urlsafe(32)
        with self._conn() as conn,conn:
            conn.execute('BEGIN IMMEDIATE');customer=self._customer(conn,sid,cid)
            if customer['username'] or customer['password']:raise RecordError('This customer already has an account. Invitations cannot reset existing credentials.')
            conn.execute('UPDATE portal_invites SET revoked_at=? WHERE studio_id=? AND customer_id=? AND used_at IS NULL AND revoked_at IS NULL',(now,sid,cid))
            conn.execute('INSERT INTO portal_invites(studio_id,customer_id,token_hash,expires_at,actor) VALUES(?,?,?,?,?)',(sid,cid,hashlib.sha256(token.encode()).hexdigest(),now+172800,str(actor)))
            self.audit(conn,sid,cid,'invite_created',actor,{'expires_at':now+172800})
            return token

    def revoke_invites(self,sid,cid,actor):
        with self._conn() as conn,conn:
            self._customer(conn,sid,cid)
            conn.execute('UPDATE portal_invites SET revoked_at=? WHERE studio_id=? AND customer_id=? AND used_at IS NULL AND revoked_at IS NULL',(int(time.time()),sid,cid))
            self.audit(conn,sid,cid,'invites_revoked',actor,{})

    def _invite(self,conn,token,now):
        if not isinstance(token,str) or len(token)!=43:raise RecordError('Invitation unavailable or expired.')
        row=conn.execute('SELECT * FROM portal_invites WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not row or row['used_at'] or row['revoked_at'] or row['expires_at']<=now:raise RecordError('Invitation unavailable or expired.')
        return row

    def accept_invite(self,token,username,password,now=None):
        import re
        now=int(time.time() if now is None else now)
        username=str(username or '').strip().lower()
        if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{3,63}',username):raise RecordError('Use a username of 4–64 letters, numbers, dots, underscores or hyphens.')
        errors=validate_password_strength(password or '')
        if errors:raise RecordError(' '.join(errors))
        if len(password)>128:raise RecordError('Password must be at most 128 characters.')
        password_hash=hash_password(password)
        with self._conn() as conn,conn:
            conn.execute('BEGIN IMMEDIATE');invite=self._invite(conn,token,now)
            customer=self._customer(conn,invite['studio_id'],invite['customer_id'])
            if customer['username'] or customer['password']:raise RecordError('An account already exists.')
            if any(conn.execute(f'SELECT 1 FROM {table} WHERE lower(username)=?',(username,)).fetchone() for table in ('studios','staff','customers')):
                raise RecordError('That username is unavailable.')
            conn.execute('UPDATE customers SET username=?,password=? WHERE studio_id=? AND id=?',(username,password_hash,invite['studio_id'],invite['customer_id']))
            conn.execute('UPDATE portal_invites SET used_at=? WHERE id=?',(now,invite['id']))
            self.audit(conn,invite['studio_id'],invite['customer_id'],'account_activated','invite_holder',{})

    def records(self,sid,cid):
        with self._conn() as conn:
            customer=self._customer(conn,sid,cid)
            def owned(table,fields,order='id DESC'):
                return [dict(r) for r in conn.execute(f'''SELECT {fields} FROM {table} r JOIN vehicles v
                  ON v.id=r.vehicle_id AND v.studio_id=r.studio_id AND v.customer_id=r.customer_id
                  WHERE r.studio_id=? AND r.customer_id=? ORDER BY r.{order}''',(sid,cid))]
            return dict(customer={'id':cid,'name':customer['name']},
                vehicles=[dict(r) for r in conn.execute('SELECT id,make_model,license_plate,vin FROM vehicles WHERE studio_id=? AND customer_id=?',(sid,cid))],
                jobs=owned('jobs','r.id,r.vehicle_id,r.car,r.service,r.status,r.completed_at'),
                estimates=owned('estimates','r.id,r.vehicle,r.status,r.total,r.created_at'),
                bookings=owned('bookings','r.id,r.vehicle,r.date,r.time_slot,r.status'),
                warranties=owned('vehicle_warranties','r.*'),
                claims=[dict(r) for r in conn.execute('''SELECT c.* FROM warranty_claims c JOIN vehicle_warranties w ON w.id=c.warranty_id AND w.studio_id=c.studio_id
                  JOIN vehicles v ON v.id=w.vehicle_id AND v.studio_id=w.studio_id AND v.customer_id=c.customer_id
                  WHERE c.studio_id=? AND c.customer_id=? ORDER BY c.id DESC''',(sid,cid))])

    def estimate(self,sid,cid,eid):
        with self._conn() as conn:
            row=conn.execute('''SELECT e.id,e.customer_name,e.vehicle,e.status,e.subtotal,e.tax_percent,e.tax_amount,e.total,e.notes,e.created_at
                 FROM estimates e JOIN vehicles v ON v.id=e.vehicle_id AND v.studio_id=e.studio_id AND v.customer_id=e.customer_id
                 WHERE e.studio_id=? AND e.customer_id=? AND e.id=?''',(sid,cid,eid)).fetchone()
            if not row:raise RecordError('Estimate not found.')
            return dict(estimate=dict(row),items=[dict(r) for r in conn.execute('SELECT name,description,quantity,unit_price,total FROM estimate_items WHERE estimate_id=? ORDER BY id',(eid,))])

    def warranty(self,sid,cid,wid):
        return next((w for w in self.records(sid,cid)['warranties'] if w['id']==wid),None)

    def issue_warranty(self,sid,jid,form,actor,key):
        self._key(key)
        fields={k:bounded(form.get(k),k.replace('_',' ').title(),4000 if k in ('coverage','exclusions','care') else 500) for k in ('title','coverage','exclusions','care','starts_on','ends_on','eligibility_note')}
        try:
            start=date.fromisoformat(fields['starts_on']);end=date.fromisoformat(fields['ends_on'])
            if end<start or (end-start).days>3650:raise ValueError()
        except ValueError:raise RecordError('Use a valid warranty period of no more than ten years.')
        fingerprint=hashlib.sha256(json.dumps([jid,fields],sort_keys=True).encode()).hexdigest()
        with self._conn() as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            prior=conn.execute('SELECT id,request_hash FROM vehicle_warranties WHERE studio_id=? AND request_key=?',(sid,key)).fetchone()
            if prior:
                if prior['request_hash']!=fingerprint:raise RecordError('This request key was used for different terms.')
                return prior['id']
            job=self._job(conn,sid,jid)
            if job['status']!='Completed' or not job['customer_id'] or not job['vehicle_id']:raise RecordError('A completed job with a verified customer and vehicle is required.')
            self.identity(conn,sid,job['vehicle_id'],job['customer_id'])
            try:completed=date.fromisoformat(str(job['completed_at'])[:10])
            except ValueError:raise RecordError('Record a valid job completion date first.')
            if start<completed:raise RecordError('Warranty cannot start before the job completion date.')
            wid=conn.execute('''INSERT INTO vehicle_warranties(studio_id,customer_id,vehicle_id,job_id,title,coverage,exclusions,care,starts_on,ends_on,eligibility_note,issued_by,request_key,request_hash)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(sid,job['customer_id'],job['vehicle_id'],jid,*[fields[k] for k in ('title','coverage','exclusions','care','starts_on','ends_on','eligibility_note')],str(actor),key,fingerprint)).lastrowid
            self.audit(conn,sid,job['customer_id'],'warranty_issued',actor,{'warranty_id':wid,'job_id':jid,'terms':fields})
            return wid

    def revoke_warranty(self,sid,wid,reason,actor):
        reason=bounded(reason,'Reason')
        with self._conn() as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT * FROM vehicle_warranties WHERE studio_id=? AND id=?',(sid,wid)).fetchone()
            if not row:raise RecordError('Warranty not found.')
            if row['status']=='Revoked':return
            conn.execute("UPDATE vehicle_warranties SET status='Revoked',revoked_reason=? WHERE id=?",(reason,wid))
            self.audit(conn,sid,row['customer_id'],'warranty_revoked',actor,{'warranty_id':wid,'reason':reason})

    def claim(self,sid,cid,wid,description,key,today=None):
        self._key(key);description=bounded(description,'Claim description',2000);today=today or date.today()
        with self._conn() as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('''SELECT w.* FROM vehicle_warranties w JOIN vehicles v ON v.id=w.vehicle_id AND v.studio_id=w.studio_id AND v.customer_id=w.customer_id
                WHERE w.studio_id=? AND w.customer_id=? AND w.id=?''',(sid,cid,wid)).fetchone()
            if not row:raise RecordError('Warranty not found.')
            prior=conn.execute('SELECT * FROM warranty_claims WHERE studio_id=? AND customer_id=? AND request_key=?',(sid,cid,key)).fetchone()
            if prior:
                if prior['warranty_id']!=wid or prior['description']!=description:raise RecordError('Request key already used.')
                return prior['id']
            if row['status']!='Active' or not row['starts_on']<=today.isoformat()<=row['ends_on']:raise RecordError('This warranty is not active today. Contact the studio to discuss your request.')
            claim=conn.execute('INSERT INTO warranty_claims(studio_id,warranty_id,customer_id,description,request_key) VALUES(?,?,?,?,?)',(sid,wid,cid,description,key)).lastrowid
            self.audit(conn,sid,cid,'claim_created',f'customer:{cid}',{'claim_id':claim,'warranty_id':wid})
            return claim

    def resolve_claim(self,sid,claim_id,status,reason,version,actor):
        reason=bounded(reason,'Customer-visible response',2000)
        transitions={'Open':('Under review','Accepted','Declined'),'Under review':('Accepted','Declined'),'Accepted':('Resolved',),'Declined':(),'Resolved':()}
        with self._conn() as conn,conn:
            conn.execute('BEGIN IMMEDIATE');row=conn.execute('SELECT * FROM warranty_claims WHERE studio_id=? AND id=?',(sid,claim_id)).fetchone()
            if not row or row['version']!=number(version):raise RecordError('Claim changed or was not found. Reload first.')
            if status not in transitions[row['status']]:raise RecordError('Invalid claim status transition.')
            conn.execute('UPDATE warranty_claims SET status=?,resolution=?,version=version+1 WHERE id=?',(status,reason,claim_id))
            self.audit(conn,sid,row['customer_id'],'claim_status',actor,{'claim_id':claim_id,'status':status,'reason':reason,'previous':row['status']})
