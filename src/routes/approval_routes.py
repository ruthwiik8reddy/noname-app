import secrets
import time
from datetime import datetime,timezone
from flask import Blueprint, abort, flash, make_response, redirect, render_template, request, session, url_for
from ..auth import roles_required
from ..services.repositories.approvals import Approvals
from ..services.repositories.job_records import RecordError,number
from .job_record_routes import actor,common,csrf_token,verify_csrf

bp=Blueprint('approvals',__name__,url_prefix='/approvals')


@bp.after_request
def private_response(response):
    response.headers['Cache-Control']='no-store'
    response.headers['Referrer-Policy']='no-referrer'
    response.headers['X-Robots-Tag']='noindex, nofollow'
    response.headers['X-Frame-Options']='DENY'
    return response


@bp.route('/manage/<int:eid>',methods=['GET','POST'])
@roles_required('admin','general_manager')
def manage(eid):
    sid=session['studio_id'];repo=Approvals();link=None
    estimate=repo.fetch_one('SELECT * FROM estimates WHERE studio_id=? AND id=?',(sid,eid))
    if not estimate:abort(404)
    if request.method=='POST':
        verify_csrf()
        try:
            if request.form.get('action')=='revoke':
                repo.revoke(sid,eid,number(request.form.get('version_id')),actor())
                flash('Link revoked.','success')
            elif request.form.get('action')=='issue':
                token,_=repo.issue(sid,eid,actor())
                link=url_for('approvals.public',token=token)
            else:abort(400)
        except RecordError as exc:flash(str(exc),'error')
    estimate=repo.fetch_one('SELECT * FROM estimates WHERE studio_id=? AND id=?',(sid,eid))
    versions=repo.fetch_all('SELECT id,created_at,expires_at,revoked_at,decision,signer FROM estimate_approval_versions WHERE studio_id=? AND estimate_id=? ORDER BY id DESC',(sid,eid))
    for version in versions:
        version['expiry_display']=datetime.fromtimestamp(version['expires_at'],timezone.utc).strftime('%d %b %Y, %H:%M UTC')
        version['expired']=version['expires_at']<=time.time()
    return render_template('approval_manage.html',**common(),estimate=estimate,link=link,csrf=csrf_token(),versions=versions)



@bp.route('/<token>',methods=['GET','POST'])
def public(token):
    repo=Approvals();session.setdefault('approval_csrf',secrets.token_urlsafe(32));error=None
    if session.get('role')=='customer':
        try:owned=repo.read(token)
        except RecordError:abort(404)
        if session.get('studio_id')!=owned['version']['studio_id'] or session.get('user_id')!=owned['est']['customer_id']:abort(404)
    if request.method=='POST':
        expected=session.get('approval_csrf','');received=request.form.get('csrf','')
        if not expected or not secrets.compare_digest(expected,received):abort(403)
        try:repo.decide(token,request.form.get('action'),request.form.get('signer'),request.form.get('confirmed'))
        except RecordError as exc:error=str(exc)
        else:return redirect(url_for('approvals.public',token=token),code=303)
    try:data=repo.read(token)
    except RecordError:return 'This approval link is unavailable. Please request a new link from the studio.',410
    if session.get('role')=='customer' and (session.get('studio_id')!=data['version']['studio_id'] or session.get('user_id')!=data['est']['customer_id']):abort(404)
    return make_response(render_template('approval_public.html',**data,error=error,csrf=session['approval_csrf']),400 if error else 200)
