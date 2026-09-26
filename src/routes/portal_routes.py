import secrets
from datetime import date
from flask import Blueprint,abort,flash,redirect,render_template,request,session,url_for
from ..auth import roles_required
from ..services.repositories.portal import Portal
from ..services.repositories.job_records import RecordError,number
from .job_record_routes import actor,common,csrf_token,verify_csrf

bp=Blueprint('portal',__name__,url_prefix='/portal')
admin_bp=Blueprint('portal_admin',__name__,url_prefix='/portal-admin')


@bp.after_request
@admin_bp.after_request
def private(response):
    response.headers['Cache-Control']='no-store';response.headers['Referrer-Policy']='no-referrer'
    return response


@bp.get('/')
@roles_required('customer')
def index():
    try:data=Portal().records(session['studio_id'],session['user_id'])
    except RecordError:abort(404)
    return render_template('portal.html',**data,csrf=csrf_token(),request_key=secrets.token_hex(24),today=date.today().isoformat())


@bp.get('/estimates/<int:eid>')
@roles_required('customer')
def estimate(eid):
    try:data=Portal().estimate(session['studio_id'],session['user_id'],eid)
    except RecordError:abort(404)
    return render_template('portal_estimate.html',**data)


@bp.get('/jobs/<int:jid>/photos')
@roles_required('customer')
def photos(jid):
    from ..security import authorize_job
    repo=Portal();sid=session['studio_id']
    job=repo.fetch_one('SELECT * FROM jobs WHERE studio_id=? AND id=?',(sid,jid));authorize_job(job,True)
    media=repo.fetch_all('SELECT id,filename,stage,caption,media_type FROM media WHERE studio_id=? AND job_id=? AND customer_visible=1 ORDER BY id',(sid,jid))
    return render_template('portal_photos.html',job=job,media=media,sid=sid)


@bp.post('/warranties/<int:wid>/claim')
@roles_required('customer')
def claim(wid):
    verify_csrf()
    try:Portal().claim(session['studio_id'],session['user_id'],wid,request.form.get('description'),request.form.get('request_key'))
    except RecordError as exc:flash(str(exc),'error')
    else:flash('Your request has been recorded for the studio to review.','success')
    return redirect(url_for('portal.index'),code=303)


@bp.route('/activate/<token>',methods=['GET','POST'])
def activate(token):
    repo=Portal();session.setdefault('activation_csrf',secrets.token_urlsafe(32));error=None
    try:
        import time
        with repo._conn() as conn:repo._invite(conn,token,int(time.time()))
    except RecordError:return 'Invitation unavailable or expired. Ask your studio for a new invitation.',410
    if request.method=='POST':
        if not secrets.compare_digest(session['activation_csrf'],request.form.get('csrf','')):abort(403)
        try:repo.accept_invite(token,request.form.get('username'),request.form.get('password'))
        except RecordError as exc:error=str(exc)
        else:return redirect(url_for('main.login',next='/portal/'),code=303)
    return render_template('portal_activate.html',csrf=session['activation_csrf'],error=error),400 if error else 200


@admin_bp.route('/',methods=['GET','POST'])
@roles_required('admin','general_manager')
def desk():
    repo=Portal();sid=session['studio_id'];invite_link=None
    if request.method=='POST':
        verify_csrf();f=request.form
        try:
            if f.get('action')=='invite':
                token=repo.invite(sid,number(f.get('customer_id')),actor());invite_link=url_for('portal.activate',token=token)
            elif f.get('action')=='revoke-invites':repo.revoke_invites(sid,number(f.get('customer_id')),actor())
            elif f.get('action')=='issue':
                if f.get('confirmed')!='yes':raise RecordError('Confirm that you checked eligibility and the displayed coverage terms.')
                repo.issue_warranty(sid,number(f.get('job_id')),dict(f),actor(),f.get('request_key'));flash('Warranty issued.','success')
            elif f.get('action')=='revoke':repo.revoke_warranty(sid,number(f.get('warranty_id')),f.get('reason'),actor())
            elif f.get('action')=='claim':repo.resolve_claim(sid,number(f.get('claim_id')),f.get('status'),f.get('reason'),f.get('version'),actor())
            elif f.get('action')=='photo':
                mid=number(f.get('media_id'));visible=f.get('visible')=='yes'
                with repo._conn() as conn,conn:
                    row=conn.execute('''SELECT m.*,j.customer_id,j.vehicle_id FROM media m JOIN jobs j ON j.id=m.job_id AND j.studio_id=m.studio_id WHERE m.studio_id=? AND m.id=?''',(sid,mid)).fetchone()
                    if not row or not row['customer_id']:raise RecordError('Choose media from a job with a verified customer.')
                    repo.identity(conn,sid,row['vehicle_id'],row['customer_id'])
                    conn.execute('UPDATE media SET customer_visible=? WHERE studio_id=? AND id=?',(int(visible),sid,mid))
                    repo.audit(conn,sid,row['customer_id'],'photo_visibility',actor(),{'media_id':mid,'visible':visible})
            else:abort(400)
        except RecordError as exc:flash(str(exc),'error')
    values=common();values['active_page']='portal_admin'
    return render_template('portal_admin.html',**values,csrf=csrf_token(),request_key=secrets.token_hex(24),invite_link=invite_link,
        customers=repo.fetch_all('SELECT id,name,username FROM customers WHERE studio_id=? ORDER BY name',(sid,)),
        jobs=repo.fetch_all("SELECT id,car,service FROM jobs WHERE studio_id=? AND status='Completed' AND vehicle_id IS NOT NULL AND customer_id IS NOT NULL ORDER BY id DESC",(sid,)),
        warranties=repo.fetch_all('SELECT * FROM vehicle_warranties WHERE studio_id=? ORDER BY id DESC',(sid,)),
        claims=repo.fetch_all('SELECT * FROM warranty_claims WHERE studio_id=? ORDER BY id DESC',(sid,)))
