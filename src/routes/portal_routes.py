import secrets
from flask import Blueprint,abort,flash,redirect,render_template,request,session,url_for
from ..auth import roles_required
from ..services.repositories.portal import Portal
from ..services.repositories.tracking_links import TrackingLinks
from ..services.repositories.job_records import RecordError,number
from .job_record_routes import actor,common,csrf_token,verify_csrf

bp=Blueprint('portal',__name__,url_prefix='/portal')
admin_bp=Blueprint('portal_admin',__name__,url_prefix='/portal-admin')


@bp.after_request
@admin_bp.after_request
def private(response):
    response.headers['X-Robots-Tag']='noindex, nofollow';response.headers['Cache-Control']='no-store';response.headers['Referrer-Policy']='no-referrer'
    return response


@bp.route('/',methods=['GET','POST'])
@bp.route('/<path:unused>',methods=['GET','POST'])
def retired_portal(unused=None):
    if request.path == '/portal/' and session.get('role') in ('admin','general_manager'):
        return redirect(url_for('portal_admin.desk'))
    return 'Customer accounts are no longer used. Ask your studio for a private job tracking link.',410


@admin_bp.route('/',methods=['GET','POST'])
@roles_required('admin','general_manager')
def desk():
    repo=Portal();sid=session['studio_id'];tracking_link=None
    if request.method=='POST':
        verify_csrf();f=request.form
        try:
            if f.get('action')=='create-link':
                token=TrackingLinks().create(sid,number(f.get('job_id')),actor())
                tracking_link=url_for('portal_admin.public_tracking',token=token,_external=True)
            elif f.get('action')=='revoke-link':
                TrackingLinks().revoke(sid,number(f.get('job_id')))
                flash('Tracking link revoked.','success')
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
    return render_template('portal_admin.html',**values,csrf=csrf_token(),request_key=secrets.token_hex(24),tracking_link=tracking_link,
        selected_customer_id=request.values.get('customer_id',type=int),

        tracking_jobs=repo.fetch_all("""SELECT j.id,j.car,j.status,
            (SELECT datetime(MAX(t.expires_at),'unixepoch') FROM job_tracking_links t
             WHERE t.studio_id=j.studio_id AND t.job_id=j.id AND t.revoked_at IS NULL
             AND t.expires_at>strftime('%s','now')) AS link_expires
            FROM jobs j WHERE j.studio_id=? AND (? IS NULL OR j.customer_id=?)
            ORDER BY j.id DESC LIMIT 100""",(sid,request.values.get('customer_id',type=int),request.values.get('customer_id',type=int))),
        jobs=repo.fetch_all("SELECT id,car,service FROM jobs WHERE studio_id=? AND status='Completed' AND vehicle_id IS NOT NULL AND customer_id IS NOT NULL ORDER BY id DESC",(sid,)),
        warranties=repo.fetch_all('SELECT * FROM vehicle_warranties WHERE studio_id=? ORDER BY id DESC',(sid,)),
        claims=repo.fetch_all('SELECT * FROM warranty_claims WHERE studio_id=? ORDER BY id DESC',(sid,)))


@admin_bp.get('/link/<token>')
def public_tracking(token):
    job=TrackingLinks().resolve(token)
    if not job:
        return render_template('tracking_link.html',job=None),410
    return render_template('tracking_link.html',job=job,token=token)


@admin_bp.get('/link/<token>/status')
def public_status(token):
    from flask import jsonify
    job=TrackingLinks().resolve(token)
    return (jsonify(job),200) if job else (jsonify(error='Tracking link unavailable.'),410)
