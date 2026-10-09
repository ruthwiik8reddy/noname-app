"""Human-reviewed follow-ups. No transport is called by this module."""
import hashlib
import json
import re
from .job_records import RecordError, number
from .vehicle_workflow import VehicleWorkflow


CHANNELS = ('email', 'sms', 'whatsapp')


def bounded(value, label, maximum=500):
    value = str(value or '').strip()
    if not value or len(value) > maximum or '\x00' in value:
        raise RecordError(f'{label} is required and must be at most {maximum} characters.')
    return value


class Followups(VehicleWorkflow):
    def _audit_followup(self, conn, sid, fid, action, actor, detail):
        conn.execute('INSERT INTO followup_audit(studio_id,followup_id,action,actor,detail) VALUES(?,?,?,?,?)',
                     (sid, fid, action, str(actor), json.dumps(detail)))

    def _customer(self, conn, sid, cid):
        customer = conn.execute('SELECT * FROM customers WHERE studio_id=? AND id=?', (sid,cid)).fetchone()
        if not customer:
            raise RecordError('Customer not found in this studio.')
        return dict(customer)

    def _destination(self, customer, channel):
        if channel not in CHANNELS:
            raise RecordError('Choose email, SMS or WhatsApp.')
        if channel == 'email':
            destination = str(customer['email'] or '').strip()
            if len(destination)>254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', destination):
                raise RecordError('Record a valid customer email address first.')
        else:
            destination = re.sub(r'[ ()\-]', '', str(customer['phone'] or ''))
            if not re.fullmatch(r'\+[1-9][0-9]{7,14}', destination):
                raise RecordError('Record a customer phone number with an explicit country code, such as +14155550123.')
        return destination

    def permission(self, sid, cid, channel, allowed, evidence, actor):
        if allowed not in ('yes','no'):
            raise RecordError('Choose allowed or opted out.')
        evidence = bounded(evidence, 'Permission evidence')
        with self._conn() as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            customer = self._customer(conn,sid,cid)
            # Opt-out must work even after a contact address was removed or invalidated.
            if channel not in CHANNELS:
                raise RecordError('Unknown contact channel.')
            destination = self._destination(customer,channel) if allowed=='yes' else ''
            previous = conn.execute('SELECT * FROM contact_permissions WHERE studio_id=? AND customer_id=? AND channel=?', (sid,cid,channel)).fetchone()
            conn.execute('''INSERT INTO contact_permissions(studio_id,customer_id,channel,destination,allowed,evidence,actor)
                 VALUES(?,?,?,?,?,?,?) ON CONFLICT(studio_id,customer_id,channel) DO UPDATE SET
                 destination=excluded.destination,allowed=excluded.allowed,evidence=excluded.evidence,
                 actor=excluded.actor,updated_at=datetime('now')''', (sid,cid,channel,destination,int(allowed=='yes'),evidence,str(actor)))
            # Revocation or renewed permission always requires a fresh review of approved drafts.
            conn.execute("UPDATE followups SET status='Draft',version=version+1,approved_at=NULL,approved_by=NULL WHERE studio_id=? AND customer_id=? AND channel=? AND status='Approved'", (sid,cid,channel))
            self._audit_followup(conn,sid,None,'contact_permission',actor,{'customer_id':cid,'channel':channel,'allowed':allowed=='yes','destination':destination,'evidence':evidence,'previous':dict(previous) if previous else None})

    def _source(self, conn, sid, vid, purpose, source_id):
        vehicle = self.identity(conn,sid,vid)
        if purpose == 'rebooking':
            record = conn.execute("SELECT * FROM jobs WHERE studio_id=? AND id=? AND vehicle_id=? AND customer_id=? AND status='Completed'", (sid,source_id,vid,vehicle['customer_id'])).fetchone()
            if not record:
                raise RecordError('Rebooking needs a completed job linked to this vehicle and customer.')
        elif purpose == 'estimate':
            record = conn.execute("SELECT * FROM estimates WHERE studio_id=? AND id=? AND vehicle_id=? AND customer_id=? AND status IN ('Draft','Sent','Revision Requested')", (sid,source_id,vid,vehicle['customer_id'])).fetchone()
            if not record:
                raise RecordError('Estimate follow-up needs an open estimate linked to this vehicle and customer.')
        else:
            raise RecordError('Choose a valid follow-up purpose.')
        return vehicle, dict(record)

    def create(self, sid, vid, purpose, source_id, channel, actor, key):
        self._key(key)
        payload = hashlib.sha256(json.dumps([vid,purpose,source_id,channel]).encode()).hexdigest()
        with self._conn() as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            prior = conn.execute('SELECT id,request_hash FROM followups WHERE studio_id=? AND request_key=?',(sid,key)).fetchone()
            if prior:
                if prior['request_hash'] != payload:
                    raise RecordError('This request key was already used with different details.')
                return prior['id']
            vehicle, source = self._source(conn,sid,vid,purpose,source_id)
            destination = self._destination(self._customer(conn,sid,vehicle['customer_id']),channel)
            if conn.execute("SELECT id FROM followups WHERE studio_id=? AND vehicle_id=? AND purpose=? AND status!='Closed'",(sid,vid,purpose)).fetchone():
                raise RecordError('An active follow-up already exists for this vehicle and purpose.')
            studio = conn.execute('SELECT name FROM studios WHERE id=?',(sid,)).fetchone()[0]
            if purpose=='estimate':
                text=f"Hello {vehicle['customer_name']}, this is {studio}. Would you like to discuss estimate #{source_id} for your {vehicle['make_model']}? Reply if you have questions or would like us to help arrange a visit."
            else:
                text=f"Hello {vehicle['customer_name']}, this is {studio}. Would you like to arrange another visit for your {vehicle['make_model']}? Reply with a convenient time and we can check availability."
            text += ' Let us know if you prefer not to receive follow-ups.'
            fid=conn.execute('''INSERT INTO followups(studio_id,customer_id,vehicle_id,purpose,source_id,channel,destination,body,request_key,request_hash)
                      VALUES(?,?,?,?,?,?,?,?,?,?)''',(sid,vehicle['customer_id'],vid,purpose,source_id,channel,destination,text,key,payload)).lastrowid
            self._audit_followup(conn,sid,fid,'draft_created',actor,{'source_id':source_id,'purpose':purpose,'body':text,'channel':channel,'destination':destination})
            return fid

    def _record(self, conn, sid, fid):
        row=conn.execute('SELECT * FROM followups WHERE studio_id=? AND id=?',(sid,fid)).fetchone()
        if not row:
            raise RecordError('Follow-up not found.')
        return dict(row)

    def _ready(self, conn, row):
        sid=row['studio_id']
        vehicle,_=self._source(conn,sid,row['vehicle_id'],row['purpose'],row['source_id'])
        if vehicle['customer_id']!=row['customer_id']:
            raise RecordError('Vehicle ownership changed. Close this draft and review the customer record.')
        destination=self._destination(self._customer(conn,sid,row['customer_id']),row['channel'])
        if destination!=row['destination']:
            raise RecordError('Contact details changed. Save the draft to refresh its recipient, then review permission again.')
        permission=conn.execute('SELECT * FROM contact_permissions WHERE studio_id=? AND customer_id=? AND channel=?',(sid,row['customer_id'],row['channel'])).fetchone()
        if not permission or not permission['allowed'] or permission['destination']!=destination:
            raise RecordError('Current permission for this channel and recipient is required.')
        if conn.execute("SELECT id FROM bookings WHERE studio_id=? AND vehicle_id=? AND date>=date('now') AND lower(status) NOT IN ('cancelled','canceled','rejected')",(sid,row['vehicle_id'])).fetchone():
            raise RecordError('This vehicle already has a future booking. Close or review the follow-up instead.')

    def change(self, sid, fid, operation, version, actor, **data):
        with self._conn() as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row=self._record(conn,sid,fid)
            if row['version']!=number(version):
                raise RecordError('This follow-up changed in another session. Reload and review it again.')
            if row['status']=='Closed':
                raise RecordError('This follow-up is closed.')
            detail={}
            if operation=='edit':
                if row['status'] not in ('Draft','Approved'):
                    raise RecordError('A contacted message cannot be edited.')
                body=bounded(data.get('body'),'Message',2000)
                destination=self._destination(self._customer(conn,sid,row['customer_id']),row['channel'])
                conn.execute("UPDATE followups SET body=?,destination=?,status='Draft',approved_at=NULL,approved_by=NULL WHERE id=?",(body,destination,fid))
                detail={'previous_body':row['body'],'body':body,'previous_destination':row['destination'],'destination':destination}
            elif operation=='approve':
                if row['status']!='Draft':
                    raise RecordError('Only a draft can be approved.')
                self._ready(conn,row)
                conn.execute("UPDATE followups SET status='Approved',approved_at=datetime('now'),approved_by=? WHERE id=?",(str(actor),fid))
                detail={'body':row['body'],'destination':row['destination'],'channel':row['channel']}
            elif operation=='contacted':
                if row['status']!='Approved' or data.get('confirmed')!='yes':
                    raise RecordError('Approve the draft and confirm that you already contacted this recipient outside BayQ.')
                self._ready(conn,row)
                reference=bounded(data.get('reference'),'External contact reference')
                floor=conn.execute('SELECT coalesce(max(id),0) FROM bookings WHERE studio_id=?',(sid,)).fetchone()[0]
                conn.execute("UPDATE followups SET status='Contacted',contacted_at=datetime('now'),contact_reference=?,contact_booking_floor=? WHERE id=?",(reference,floor,fid))
                detail={'reference':reference,'delivery_source':'operator_attestation'}
            elif operation=='outcome':
                if row['status']!='Contacted':
                    raise RecordError('Record external contact before attributing a booking.')
                bid=number(data.get('booking_id'))
                booking=conn.execute('''SELECT * FROM bookings WHERE studio_id=? AND id=? AND customer_id=? AND vehicle_id=?
                     AND created_at>=? AND id>? AND lower(status) NOT IN ('cancelled','canceled','rejected')''',
                     (sid,bid,row['customer_id'],row['vehicle_id'],row['contacted_at'],row['contact_booking_floor'])).fetchone()
                if not booking:
                    raise RecordError('Select an active booking created after contact for this customer and vehicle.')
                if conn.execute('SELECT id FROM followups WHERE studio_id=? AND booking_id=?',(sid,bid)).fetchone():
                    raise RecordError('That booking is already attributed to a follow-up.')
                reason=bounded(data.get('reason'),'Attribution evidence')
                conn.execute("UPDATE followups SET status='Closed',outcome='Booked',booking_id=?,closed_reason=? WHERE id=?",(bid,reason,fid))
                detail={'booking_id':bid,'reason':reason,'attribution':'operator_reported'}
            elif operation=='close':
                reason=bounded(data.get('reason'),'Closure reason')
                conn.execute("UPDATE followups SET status='Closed',outcome='Closed without attribution',closed_reason=? WHERE id=?",(reason,fid))
                detail={'reason':reason}
            else:
                raise RecordError('Unknown follow-up action.')
            conn.execute('UPDATE followups SET version=version+1 WHERE id=?',(fid,))
            self._audit_followup(conn,sid,fid,operation,actor,detail)

    def detail(self, sid, fid):
        with self._conn() as conn:
            row=self._record(conn,sid,fid)
            blocked=None
            try:self._ready(conn,row)
            except RecordError as exc:blocked=str(exc)
            customer=self._customer(conn,sid,row['customer_id'])
            return dict(record=row,customer=customer,blocked=blocked,
                permissions=[dict(r) for r in conn.execute('SELECT * FROM contact_permissions WHERE studio_id=? AND customer_id=?',(sid,row['customer_id']))],
                history=[dict(r) for r in conn.execute('SELECT * FROM followup_audit WHERE studio_id=? AND followup_id=? ORDER BY id DESC',(sid,fid))],
                bookings=[dict(r) for r in conn.execute('SELECT id,date,status,created_at FROM bookings WHERE studio_id=? AND vehicle_id=? AND customer_id=? ORDER BY id DESC LIMIT 100',(sid,row['vehicle_id'],row['customer_id']))])

    def queue(self, sid):
        return self.fetch_all('''SELECT f.*,c.name AS customer_name,v.make_model,b.status AS booking_status FROM followups f
           JOIN customers c ON c.studio_id=f.studio_id AND c.id=f.customer_id
           JOIN vehicles v ON v.studio_id=f.studio_id AND v.id=f.vehicle_id
           LEFT JOIN bookings b ON b.studio_id=f.studio_id AND b.id=f.booking_id
           WHERE f.studio_id=? ORDER BY f.id DESC LIMIT 200''',(sid,))

    def candidates(self, sid):
        # Deterministic review candidates, not predictions of when maintenance is due.
        return self.fetch_all('''SELECT v.id AS vehicle_id,v.make_model,c.name AS customer_name,
          j.id AS source_id,'rebooking' AS purpose,j.completed_at AS source_date
          FROM vehicles v JOIN customers c ON c.id=v.customer_id AND c.studio_id=v.studio_id
          JOIN jobs j ON j.vehicle_id=v.id AND j.studio_id=v.studio_id AND j.customer_id=c.id
          WHERE v.studio_id=? AND j.status='Completed' AND datetime(j.completed_at)<=datetime('now','-90 days')
          AND NOT EXISTS(SELECT 1 FROM jobs newer WHERE newer.studio_id=v.studio_id AND newer.vehicle_id=v.id
            AND newer.status='Completed' AND (datetime(newer.completed_at)>datetime(j.completed_at) OR
                (newer.completed_at=j.completed_at AND newer.id>j.id)))
          AND NOT EXISTS(SELECT 1 FROM bookings b WHERE b.studio_id=v.studio_id AND b.vehicle_id=v.id AND b.date>=date('now') AND lower(b.status) NOT IN ('cancelled','canceled','rejected'))
          AND NOT EXISTS(SELECT 1 FROM followups f WHERE f.studio_id=v.studio_id AND f.vehicle_id=v.id AND f.purpose='rebooking' AND (f.status!='Closed' OR f.created_at>=datetime('now','-30 days')))
          UNION ALL
          SELECT v.id,v.make_model,c.name,e.id,'estimate',e.created_at FROM estimates e
          JOIN vehicles v ON v.id=e.vehicle_id AND v.studio_id=e.studio_id AND v.customer_id=e.customer_id
          JOIN customers c ON c.id=e.customer_id AND c.studio_id=e.studio_id
          WHERE e.studio_id=? AND e.status IN ('Draft','Sent','Revision Requested') AND datetime(e.created_at)<=datetime('now','-7 days')
          AND NOT EXISTS(SELECT 1 FROM bookings b WHERE b.studio_id=v.studio_id AND b.vehicle_id=v.id AND b.date>=date('now') AND lower(b.status) NOT IN ('cancelled','canceled','rejected'))
          AND NOT EXISTS(SELECT 1 FROM followups f WHERE f.studio_id=v.studio_id AND f.vehicle_id=v.id AND f.purpose='estimate' AND (f.status!='Closed' OR f.created_at>=datetime('now','-30 days')))
          ORDER BY source_date LIMIT 100''',(sid,sid))
