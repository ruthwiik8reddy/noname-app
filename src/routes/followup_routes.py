"""Manager review desk. Contact recording is an attestation, never a send action."""
import secrets
from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from ..auth import roles_required
from .job_record_routes import actor, common, csrf_token, verify_csrf
from ..services.repositories.followups import Followups
from ..services.repositories.job_records import RecordError, number

bp=Blueprint('followups',__name__,url_prefix='/followups')


def context():
    values=common();values['active_page']='followups';return values


@bp.get('/')
@roles_required('admin','general_manager')
def index():
    repo=Followups();sid=session['studio_id']
    return render_template('followups.html',**context(),records=repo.queue(sid),candidates=repo.candidates(sid),
        choices=repo.choices(sid),csrf=csrf_token(),request_key=secrets.token_hex(24))


@bp.post('/new')
@roles_required('admin','general_manager')
def create():
    verify_csrf();f=request.form
    try:
        fid=Followups().create(session['studio_id'],number(f.get('vehicle_id')),f.get('purpose'),number(f.get('source_id')),f.get('channel'),actor(),f.get('request_key'))
    except RecordError as exc:
        flash(str(exc),'error');return redirect(url_for('followups.index'),code=303)
    return redirect(url_for('followups.detail',fid=fid),code=303)


@bp.get('/<int:fid>')
@roles_required('admin','general_manager')
def detail(fid):
    try:data=Followups().detail(session['studio_id'],fid)
    except RecordError:abort(404)
    return render_template('followup_detail.html',**context(),**data,csrf=csrf_token())


@bp.post('/<int:fid>/<operation>')
@roles_required('admin','general_manager')
def change(fid,operation):
    verify_csrf();repo=Followups();sid=session['studio_id'];f=request.form
    if not repo.fetch_one('SELECT id FROM followups WHERE studio_id=? AND id=?',(sid,fid)):abort(404)
    try:
        repo.change(sid,fid,operation,f.get('version'),actor(),body=f.get('body'),confirmed=f.get('confirmed'),reference=f.get('reference'),booking_id=f.get('booking_id'),reason=f.get('reason'))
    except RecordError as exc:flash(str(exc),'error')
    else:flash('Follow-up record saved.','success')
    return redirect(url_for('followups.detail',fid=fid),code=303)


@bp.post('/<int:fid>/permission')
@roles_required('admin','general_manager')
def permission(fid):
    verify_csrf();repo=Followups();sid=session['studio_id']
    record=repo.fetch_one('SELECT * FROM followups WHERE studio_id=? AND id=?',(sid,fid))
    if not record:abort(404)
    try:repo.permission(sid,record['customer_id'],record['channel'],request.form.get('allowed'),request.form.get('evidence'),actor())
    except RecordError as exc:flash(str(exc),'error')
    else:flash('Contact preference recorded. Approved drafts require review again.','success')
    return redirect(url_for('followups.detail',fid=fid),code=303)
