"""Verified identities across estimates, appointments and idempotent job conversion."""
from datetime import date, time, datetime
import hashlib
import json
from .job_records import JobRecords, RecordError, number
from .scheduling import duration_minutes, ensure_available


class VehicleWorkflow(JobRecords):
    def identity(self,conn,sid,vid,customer_id=None):
        vehicle=conn.execute('''SELECT v.*,c.name AS customer_name,c.phone AS customer_phone,c.email AS customer_email
            FROM vehicles v JOIN customers c ON c.id=v.customer_id AND c.studio_id=v.studio_id
            WHERE v.id=? AND v.studio_id=?''',(number(vid),sid)).fetchone()
        if not vehicle or (customer_id is not None and vehicle['customer_id']!=number(customer_id)):
            raise RecordError('Select a vehicle belonging to this customer and studio.')
        return dict(vehicle)

    def choices(self,sid):
        return self.fetch_all('''SELECT v.id,v.customer_id,v.make_model,v.license_plate,v.vin,c.name,c.phone FROM vehicles v
            JOIN customers c ON c.id=v.customer_id AND c.studio_id=v.studio_id WHERE v.studio_id=? ORDER BY c.name,v.id''',(sid,))

    def book(self,sid,form,actor):
        self._key(form.get('request_key'))
        try:
            appointment=date.fromisoformat(form.get('date',''))
            raw_slot=form.get('time_slot','')
            try:slot=time.fromisoformat(raw_slot)
            except ValueError:slot=datetime.strptime(raw_slot,'%I:%M %p').time()
            if slot.tzinfo or slot.second or slot.microsecond:raise ValueError()
            slot_label=slot.strftime('%I:%M %p').lstrip('0')
        except (ValueError,TypeError):
            raise RecordError('Select a valid date and time.')
        service_id=number(form.get('service_id'));bay_id=number(form['bay_id']) if form.get('bay_id') else None
        payload_hash=hashlib.sha256(json.dumps({k:str(form.get(k,'')).strip() for k in ('customer_name','customer_phone','vehicle','vehicle_id','estimate_id','service_id','bay_id','date','time_slot','notes')},sort_keys=True).encode()).hexdigest()
        with self._conn() as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            prior=conn.execute('SELECT id,request_hash FROM bookings WHERE studio_id=? AND request_key=?',(sid,form['request_key'])).fetchone()
            if prior:
                if prior['request_hash']!=payload_hash:raise RecordError('This submission was already saved with different details. Start a new booking.')
                return prior['id']
            service=conn.execute('SELECT id,duration_hr FROM services WHERE id=? AND studio_id=?',(service_id,sid)).fetchone()
            if not service:raise RecordError('Select a service in this studio.')
            minutes=duration_minutes(service['duration_hr'])
            ensure_available(conn,sid,bay_id,appointment.isoformat(),slot_label,minutes)
            estimate_id=number(form['estimate_id']) if form.get('estimate_id') else None
            vehicle_id=number(form['vehicle_id']) if form.get('vehicle_id') else None
            customer_id=None
            if estimate_id:
                estimate=conn.execute('SELECT * FROM estimates WHERE id=? AND studio_id=?',(estimate_id,sid)).fetchone()
                if not estimate or estimate['status']!='Approved':raise RecordError('Choose an approved estimate from this studio.')
                from .approvals import Approvals
                Approvals(connection=conn).verify_approved(conn,sid,estimate_id)
                if not estimate['vehicle_id']:raise RecordError('Link a verified vehicle to the estimate before booking it.')
                if vehicle_id and vehicle_id!=estimate['vehicle_id']:raise RecordError('The selected vehicle differs from the approved estimate.')
                vehicle_id=estimate['vehicle_id'];customer_id=estimate['customer_id']
            if vehicle_id:
                v=self.identity(conn,sid,vehicle_id,customer_id)
                customer_id=v['customer_id'];name=v['customer_name'];phone=v['customer_phone'] or '';description=v['make_model']
            else:
                name=str(form.get('customer_name','')).strip();phone=str(form.get('customer_phone','')).strip();description=str(form.get('vehicle','')).strip()
                if not name or not phone or not description:raise RecordError('Customer name, phone and vehicle description are required.')
                matches=conn.execute('SELECT id FROM customers WHERE studio_id=? AND phone=?',(sid,phone)).fetchall()
                if len(matches)>1:raise RecordError('Multiple customers share this phone. Select their verified vehicle instead.')
                customer_id=matches[0]['id'] if matches else conn.execute('INSERT INTO customers(studio_id,name,phone) VALUES(?,?,?)',(sid,name,phone)).lastrowid
            booking=conn.execute('''INSERT INTO bookings(studio_id,customer_name,customer_phone,vehicle,service_id,bay_id,date,time_slot,notes,status,customer_id,estimate_id,vehicle_id,request_key,request_hash,duration_minutes)
                VALUES(?,?,?,?,?,?,?,?,?,'Pending',?,?,?,?,?,?)''',
                (sid,name,phone,description,service_id,bay_id,appointment.isoformat(),slot_label,str(form.get('notes',''))[:2000],customer_id,estimate_id,vehicle_id,form['request_key'],payload_hash,minutes)).lastrowid
            self._audit(conn,sid,None,'booking_created',actor,{'booking_id':booking,'vehicle_id':vehicle_id,'estimate_id':estimate_id})
            return booking

    def link_record(self,sid,kind,record_id,vehicle_id,actor):
        if kind not in ('estimates','bookings'):raise RecordError('Unknown record type.')
        with self._conn() as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            record=conn.execute(f'SELECT * FROM {kind} WHERE studio_id=? AND id=?',(sid,record_id)).fetchone()
            if not record:raise RecordError('Record not found.')
            if kind=='estimates' and record['status']=='Approved' and record['vehicle_id'] is not None and record['vehicle_id']!=vehicle_id:
                raise RecordError('An approved estimate cannot be switched to another vehicle. Create a revised estimate.')
            if kind=='bookings' and record['estimate_id']:
                source=conn.execute('SELECT vehicle_id FROM estimates WHERE studio_id=? AND id=?',(sid,record['estimate_id'])).fetchone()
                if not source or source['vehicle_id']!=vehicle_id:raise RecordError('Link the vehicle on the source estimate first; identities must agree.')
            children=(conn.execute('SELECT vehicle_id FROM bookings WHERE studio_id=? AND estimate_id=?',(sid,record_id)).fetchall() if kind=='estimates'
                      else conn.execute('SELECT vehicle_id FROM jobs WHERE studio_id=? AND booking_id=?',(sid,record_id)).fetchall())
            if any(r['vehicle_id'] is not None and r['vehicle_id']!=vehicle_id for r in children):raise RecordError('This record already has downstream work. Its vehicle cannot be changed independently.')
            v=self.identity(conn,sid,vehicle_id,record['customer_id'])
            conn.execute(f'UPDATE {kind} SET vehicle_id=?,customer_id=?,vehicle=? WHERE studio_id=? AND id=?',(vehicle_id,v['customer_id'],v['make_model'],sid,record_id))
            self._audit(conn,sid,None,'vehicle_linked_'+kind,actor,{'record_id':record_id,'previous_vehicle_id':record['vehicle_id'],'vehicle_id':vehicle_id})

    def convert_booking(self,sid,bid,actor):
        with self._conn() as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            booking=conn.execute('SELECT * FROM bookings WHERE studio_id=? AND id=?',(sid,bid)).fetchone()
            if not booking:raise RecordError('Booking not found.')
            existing=conn.execute('SELECT id FROM jobs WHERE studio_id=? AND booking_id=?',(sid,bid)).fetchone()
            if existing:return existing['id']
            if booking['status'] not in ('Pending','Confirmed'):raise RecordError('Only pending or confirmed bookings can become jobs.')
            if not booking['vehicle_id']:raise RecordError('Select a verified vehicle before creating the job.')
            v=self.identity(conn,sid,booking['vehicle_id'],booking['customer_id'])
            service=conn.execute('SELECT name,price FROM services WHERE studio_id=? AND id=?',(sid,booking['service_id'])).fetchone()
            if not service:raise RecordError('The booked service no longer exists in this studio.')
            name,price=service['name'],service['price']
            if booking['estimate_id']:
                estimate=conn.execute('SELECT * FROM estimates WHERE studio_id=? AND id=?',(sid,booking['estimate_id'])).fetchone()
                if not estimate or estimate['status']!='Approved' or estimate['vehicle_id']!=v['id'] or estimate['customer_id']!=v['customer_id']:
                    raise RecordError('The approved estimate no longer matches this booking.')
                from .approvals import Approvals
                Approvals(connection=conn).verify_approved(conn,sid,booking['estimate_id'])
                name=estimate['services_summary'] or name
                # Work selling price excludes tax. SQLite preserves fractional dollar values despite INTEGER affinity.
                price=estimate['subtotal']/100
            job=conn.execute('''INSERT INTO jobs(studio_id,car,service,status,technician,price,customer_id,vehicle_id,booking_id,estimate_id)
                VALUES(?,?,?,'Pending','Unassigned',?,?,?,?,?)''',(sid,v['make_model'],name,price,v['customer_id'],v['id'],bid,booking['estimate_id'])).lastrowid
            conn.execute("INSERT INTO job_status_history(studio_id,job_id,from_status,to_status,actor,note) VALUES(?,?,'','Pending',?,'Created from booking')",(sid,job,actor))
            conn.execute("UPDATE bookings SET status='Confirmed' WHERE studio_id=? AND id=?",(sid,bid))
            self._audit(conn,sid,job,'booking_converted',actor,{'booking_id':bid,'estimate_id':booking['estimate_id'],'vehicle_id':v['id']})
            return job

    def reservation_status(self,sid,bid,status,actor):
        transitions={'Pending':{'Confirmed','Cancelled'},'Confirmed':{'Completed','Cancelled'},
                     'Cancelled':{'Pending','Confirmed'},'Completed':set()}
        with self._conn() as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT * FROM bookings WHERE studio_id=? AND id=?',(sid,bid)).fetchone()
            if not row:raise RecordError('Booking not found.')
            if status==row['status']:return
            if status not in transitions.get(row['status'],set()):
                raise RecordError('This booking status transition is not allowed.')
            if status in ('Pending','Confirmed'):
                if conn.execute('SELECT id FROM jobs WHERE studio_id=? AND booking_id=?',(sid,bid)).fetchone():
                    raise RecordError('This booking already has a job. Review its work record instead of reactivating it.')
                service=conn.execute('SELECT duration_hr FROM services WHERE studio_id=? AND id=?',(sid,row['service_id'])).fetchone()
                minutes=row['duration_minutes'] or duration_minutes(service['duration_hr'] if service else None)
                ensure_available(conn,sid,row['bay_id'],row['date'],row['time_slot'],minutes,bid)
            conn.execute('UPDATE bookings SET status=? WHERE studio_id=? AND id=?',(status,sid,bid))
            self._audit(conn,sid,None,'booking_status',actor,{'booking_id':bid,'previous':row['status'],'status':status})
