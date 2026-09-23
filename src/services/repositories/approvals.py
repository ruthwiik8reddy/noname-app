"""Capability links for frozen quote versions. Raw tokens are returned once, never stored."""
import hashlib
import json
import secrets
import time
from .vehicle_workflow import VehicleWorkflow
from .job_records import RecordError, number, rounded
from decimal import Decimal, InvalidOperation
from .followups import bounded


class Approvals(VehicleWorkflow):
    def _snapshot(self,conn,sid,eid):
        row=conn.execute('SELECT * FROM estimates WHERE studio_id=? AND id=?',(sid,eid)).fetchone()
        if not row:raise RecordError('Estimate not found.')
        fields=('id','customer_id','vehicle_id','customer_name','vehicle','subtotal','tax_percent','tax_amount','total','notes','services_summary','created_at')
        estimate={key:row[key] for key in fields}
        items=[dict(r) for r in conn.execute('SELECT name,description,quantity,unit_price,total FROM estimate_items WHERE estimate_id=? ORDER BY id',(eid,))]
        snapshot=json.dumps({'estimate':estimate,'items':items},sort_keys=True,separators=(',',':'))
        return row,snapshot,hashlib.sha256(snapshot.encode()).hexdigest()

    def issue(self,sid,eid,actor,now=None):
        now=int(time.time() if now is None else now)
        token=secrets.token_urlsafe(32)
        with self._conn() as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            estimate,snapshot,fingerprint=self._snapshot(conn,sid,eid)
            if estimate['status'] not in ('Draft','Sent','Revision Requested'):
                raise RecordError('Approved estimates cannot be shared as a new version. Create a revised estimate.')
            if not estimate['customer_id'] or not estimate['vehicle_id']:
                raise RecordError('Link a verified customer and vehicle before sharing.')
            self.identity(conn,sid,estimate['vehicle_id'],estimate['customer_id'])
            items=json.loads(snapshot)['items']
            try:
                subtotal=number(estimate['subtotal']);tax=number(estimate['tax_amount']);total=number(estimate['total'])
                percent=Decimal(str(estimate['tax_percent']))
                if not percent.is_finite() or not 0<=percent<=100:raise ValueError()
                amounts=[]
                for item in items:
                    qty=Decimal(str(item['quantity']));price=number(item['unit_price']);line=number(item['total'])
                    if not qty.is_finite() or not 0<qty<=1_000_000 or rounded(qty*price)!=line:raise ValueError()
                    amounts.append(line)
                if not amounts or sum(amounts)!=subtotal or total!=subtotal+tax or rounded(Decimal(subtotal)*percent/100)!=tax:raise ValueError()
            except (ValueError,InvalidOperation,TypeError):
                raise RecordError('Quote lines, quantities, tax and totals must reconcile before sharing.')
            conn.execute('UPDATE estimate_approval_versions SET revoked_at=? WHERE studio_id=? AND estimate_id=? AND revoked_at IS NULL AND decided_at IS NULL',(now,sid,eid))
            version=conn.execute('''INSERT INTO estimate_approval_versions(studio_id,estimate_id,token_hash,snapshot,fingerprint,created_at,expires_at,created_by)
                    VALUES(?,?,?,?,?,?,?,?)''',(sid,eid,hashlib.sha256(token.encode()).hexdigest(),snapshot,fingerprint,now,now+7*86400,str(actor))).lastrowid
            conn.execute("UPDATE estimates SET status='Sent' WHERE studio_id=? AND id=?",(sid,eid))
            self._audit(conn,sid,None,'approval_link_created',actor,{'estimate_id':eid,'version_id':version,'expires_at':now+7*86400})
            return token,version

    def revoke(self,sid,eid,version,actor,now=None):
        now=int(time.time() if now is None else now)
        with self._conn() as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT * FROM estimate_approval_versions WHERE id=? AND studio_id=? AND estimate_id=?',(version,sid,eid)).fetchone()
            if not row:raise RecordError('Approval version not found.')
            if row['decided_at'] is not None:raise RecordError('A recorded customer decision cannot be revoked here.')
            conn.execute('UPDATE estimate_approval_versions SET revoked_at=? WHERE id=?',(now,version))
            self._audit(conn,sid,None,'approval_link_revoked',actor,{'estimate_id':eid,'version_id':version})

    def _token(self,conn,token,now):
        if not isinstance(token,str) or len(token)!=43:raise RecordError('This approval link is unavailable or expired.')
        row=conn.execute('SELECT * FROM estimate_approval_versions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not row or row['revoked_at'] is not None or row['expires_at']<=now:
            raise RecordError('This approval link is unavailable or expired.')
        row=dict(row)
        if row['decided_at'] is None:
            estimate,_,fingerprint=self._snapshot(conn,row['studio_id'],row['estimate_id'])
            if estimate['status']!='Sent' or fingerprint!=row['fingerprint']:
                raise RecordError('This quote changed. Ask the studio for a new approval link.')
            self.identity(conn,row['studio_id'],estimate['vehicle_id'],estimate['customer_id'])
        return row

    def read(self,token,now=None):
        with self._conn() as conn:
            row=self._token(conn,token,int(time.time() if now is None else now))
            studio=conn.execute('SELECT name FROM studios WHERE id=?',(row['studio_id'],)).fetchone()
            data=json.loads(row['snapshot']);data['estimate']['studio_name']=studio['name'] if studio else 'Studio'
            return dict(version=row,est=data['estimate'],items=data['items'])

    def decide(self,token,action,signer,confirmed,now=None):
        now=int(time.time() if now is None else now)
        if action not in ('approve','revision'):raise RecordError('Choose approve or request changes.')
        signer=bounded(signer,'Your name',120)
        if action=='approve' and confirmed!='yes':raise RecordError('Confirm that you approve the displayed services and price.')
        with self._conn() as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            row=self._token(conn,token,now)
            if row['decided_at'] is not None:
                if row['decision']!=action or row['signer']!=signer:
                    raise RecordError('This quote version already has a recorded response.')
                return row['decision']
            conn.execute('UPDATE estimate_approval_versions SET decided_at=?,decision=?,signer=? WHERE id=?',(now,action,signer,row['id']))
            status='Approved' if action=='approve' else 'Revision Requested'
            conn.execute("UPDATE estimates SET status=?,signature=?,approved_at=? WHERE studio_id=? AND id=?",(status,signer if action=='approve' else '',time.strftime('%Y-%m-%d %H:%M:%S',time.gmtime(now)) if action=='approve' else '',row['studio_id'],row['estimate_id']))
            self._audit(conn,row['studio_id'],None,'customer_quote_response','customer_link',{'estimate_id':row['estimate_id'],'version_id':row['id'],'decision':action,'signer':signer,'fingerprint':row['fingerprint']})
            return action

    def verify_approved(self,conn,sid,eid):
        row=conn.execute("SELECT fingerprint FROM estimate_approval_versions WHERE studio_id=? AND estimate_id=? AND decision='approve' ORDER BY id DESC LIMIT 1",(sid,eid)).fetchone()
        if row and self._snapshot(conn,sid,eid)[2]!=row['fingerprint']:
            raise RecordError('The estimate differs from its approved version. Create and approve a revised estimate.')
