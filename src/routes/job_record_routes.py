"""Authenticated job execution records with manager-only costing and session CSRF."""
import secrets
from flask import Blueprint, abort, jsonify, redirect, render_template, request, session, url_for, flash
from ..auth import auth_context, roles_required
from ..services.repositories.job_records import JobRecords, RecordError, number

bp=Blueprint('job_records',__name__,url_prefix='/work-records')


def csrf_token():
    if session.get('logged_in'):
        session.setdefault('work_records_csrf',secrets.token_urlsafe(32))
    return session.get('work_records_csrf','')


def verify_csrf():
    expected=session.get('work_records_csrf','')
    received=request.headers.get('X-CSRF-Token') or request.form.get('csrf','')
    if not expected or not secrets.compare_digest(expected,received):
        abort(403)


def manager():
    return session.get('role') in ('admin','general_manager')


def actor():
    return f"{session.get('role')}:{session.get('staff_id') or session.get('user_id')}"


def authorize(jid,starting=False):
    sid=session['studio_id'];repo=JobRecords()
    job=repo.fetch_one('SELECT * FROM jobs WHERE studio_id=? AND id=?',(sid,jid))
    if not job:
        abort(404)
    if not manager():
        staff=session.get('staff_id')
        own_timer=repo.fetch_one('SELECT id FROM job_labor WHERE studio_id=? AND job_id=? AND staff_id=? AND ended_at IS NULL',(sid,jid,staff))
        if not staff or (job['assigned_staff_id']!=staff and (starting or not own_timer)):
            abort(403)
    return job


def common():
    return dict(**auth_context(),studio=session.get('studio'),city=session.get('city'),owner=session.get('owner'),active_page='dispatch')


@bp.get('/jobs/<int:jid>')
@roles_required('admin','general_manager','technician')
def detail(jid):
    authorize(jid);sid=session['studio_id'];repo=JobRecords()
    data=repo.detail(sid,jid)
    return render_template('job_records.html',**common(),**data,is_manager=manager(),csrf=csrf_token(),
       request_key=secrets.token_hex(24),current_staff=session.get('staff_id'),
       staff=repo.fetch_all('''SELECT s.id,s.name,r.hourly_cents FROM staff s LEFT JOIN staff_cost_rates r ON r.staff_id=s.id AND r.studio_id=s.studio_id
                            WHERE s.studio_id=? ORDER BY s.name''',(sid,)) if manager() else [],
       vehicles=repo.fetch_all('''SELECT v.id,v.make_model,v.license_plate,v.vin,c.name AS customer_name FROM vehicles v
                  JOIN customers c ON c.id=v.customer_id AND c.studio_id=v.studio_id WHERE v.studio_id=?
                  AND (? IS NULL OR v.customer_id=?) ORDER BY v.make_model''',(sid,data['job']['customer_id'],data['job']['customer_id'])) if manager() else [],
       inventory=repo.fetch_all('SELECT id,name,quantity,unit FROM inventory_items WHERE studio_id=? ORDER BY name',(sid,)))


@bp.post('/jobs/<int:jid>/<operation>')
@roles_required('admin','general_manager','technician')
def mutate(jid,operation):
    verify_csrf();authorize(jid,starting=operation=='start');repo=JobRecords();sid=session['studio_id'];f=request.form
    if operation not in ('start','stop','consume') and not manager():
        abort(403)
    try:
        if operation=='link':
            repo.link_vehicle(sid,jid,number(f.get('vehicle_id')),actor())
        elif operation=='rate':
            repo.set_rate(sid,number(f.get('staff_id')),f.get('hourly'),actor())
        elif operation=='plan':
            repo.plan(sid,jid,f.get('minutes'),f.get('materials'),actor())
        elif operation=='start':
            staff_id=number(f.get('staff_id')) if manager() else session['staff_id']
            repo.start(sid,jid,staff_id,actor(),f.get('request_key'))
        elif operation=='stop':
            repo.stop(sid,jid,number(f.get('timer_id')),actor(),None if manager() else session['staff_id'])
        elif operation in ('consume','return'):
            repo.material(sid,jid,number(f.get('item_id')),f.get('quantity'),actor(),f.get('request_key'),
                          f.get('reason','Used on job'),number(f.get('return_of')) if operation=='return' else None)
        elif operation=='correct-cost':
            repo.correct_cost(sid,jid,f.get('kind'),number(f.get('entry_id')),f.get('rate'),actor(),f.get('reason'))
        elif operation=='review':
            if f.get('confirmed')!='yes':
                raise RecordError('Confirm that all labor and material records have been checked.')
            repo.review(sid,jid,actor())
        else:
            abort(404)
    except RecordError as exc:
        flash(str(exc),'error')
    else:
        flash('Work record saved.','success')
    return redirect(url_for('job_records.detail',jid=jid),code=303)


@bp.get('/vehicles/<int:vid>')
@roles_required('admin','general_manager')
def vehicle(vid):
    try:
        data=JobRecords().timeline(session['studio_id'],vid)
    except RecordError:
        abort(404)
    return render_template('vehicle_history.html',**common(),**data)


def legacy_material_scan():
    """Keep the existing scanner, but refuse ambiguous jobs and use the atomic ledger."""
    verify_csrf()
    data=request.get_json(silent=True)
    if not isinstance(data,dict):
        return jsonify(error='A JSON object is required.'),400
    sid=session['studio_id'];repo=JobRecords();staff=session.get('staff_id')
    if session.get('role')!='technician' or not staff:
        return jsonify(error='Use the work-record page to select a job.'),403
    jobs=repo.fetch_all("SELECT id FROM jobs WHERE studio_id=? AND assigned_staff_id=? AND status='In Progress'",(sid,staff))
    if len(jobs)!=1:
        return jsonify(error='Open the work-record page for the specific job. The scanner needs exactly one active assigned job.'),409
    item=repo.fetch_one('SELECT id FROM inventory_items WHERE studio_id=? AND sku=?',(sid,data.get('sku')))
    if not item:
        return jsonify(error='Inventory item not found.'),404
    try:
        repo.material(sid,jobs[0]['id'],item['id'],data.get('quantity_used'),actor(),data.get('request_key'))
    except RecordError as exc:
        return jsonify(error=str(exc)),400
    return jsonify(success=True,message=f"Material recorded for job #{jobs[0]['id']}.")


@bp.route('/identity/<kind>/<int:record_id>',methods=['GET','POST'])
@roles_required('admin','general_manager')
def record_identity(kind,record_id):
    from ..services.repositories.vehicle_workflow import VehicleWorkflow
    if kind not in ('bookings','estimates'):abort(404)
    repo=VehicleWorkflow();sid=session['studio_id']
    record=repo.fetch_one(f'SELECT * FROM {kind} WHERE id=? AND studio_id=?',(record_id,sid))
    if not record:abort(404)
    if request.method=='POST':
        verify_csrf()
        try:repo.link_record(sid,kind,record_id,number(request.form.get('vehicle_id')),actor())
        except RecordError as exc:flash(str(exc),'error')
        else:flash('Verified vehicle linked.','success')
        return redirect(request.path,code=303)
    choices=[v for v in repo.choices(sid) if record['customer_id'] is None or v['customer_id']==record['customer_id']]
    return render_template('record_identity.html',**common(),kind=kind,record=record,choices=choices,csrf=csrf_token())


@bp.post('/bookings/<int:bid>/job')
@roles_required('admin','general_manager')
def booking_job(bid):
    from ..services.repositories.vehicle_workflow import VehicleWorkflow
    verify_csrf()
    try:jid=VehicleWorkflow().convert_booking(session['studio_id'],bid,actor())
    except RecordError as exc:
        flash(str(exc),'error')
        return redirect(url_for('job_records.record_identity',kind='bookings',record_id=bid),code=303)
    return redirect(url_for('job_records.detail',jid=jid),code=303)
