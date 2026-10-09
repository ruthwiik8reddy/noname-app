"""Application security boundaries for customer sessions and private media."""
import json
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from flask import abort, g, request, session, send_file
from .config import Config
from .db_manager import get_db


def safe_next(value,default='/dashboard'):
    if not isinstance(value,str) or not value.startswith('/') or value.startswith('//') or '\\' in value or any(ord(c)<32 for c in value):
        return default
    parsed=urlsplit(value)
    return default if parsed.scheme or parsed.netloc else value


def authorize_job(job,customer_visible=False):
    if not job or not session.get('logged_in') or job['studio_id']!=session.get('studio_id'):abort(404)
    role=session.get('role')
    if role in ('admin','general_manager','service_advisor'):return
    if role=='customer':
        if not customer_visible or job['customer_id']!=session.get('user_id'):abort(404)
        vehicle=get_db().execute('SELECT id FROM vehicles WHERE studio_id=? AND id=? AND customer_id=?',(job['studio_id'],job['vehicle_id'],session['user_id'])).fetchone()
        if not vehicle:abort(404)
        return
    if role in ('technician','photographer') and job['assigned_staff_id']==session.get('staff_id'):return
    abort(403)


def private_media(studio_id,filename):
    relative=Path(filename)
    if relative.suffix.lower() not in ('.png','.jpg','.jpeg','.gif','.webp','.mp4','.mov','.avi'):abort(404)
    if relative.is_absolute() or '..' in relative.parts or '\\' in filename:abort(404)
    if not session.get('logged_in') or session.get('studio_id')!=studio_id:abort(404)
    conn=get_db();job=None;visible=False
    if len(relative.parts)==1:
        media=conn.execute('SELECT * FROM media WHERE studio_id=? AND filename=?',(studio_id,filename)).fetchone()
        if media:
            job=conn.execute('SELECT * FROM jobs WHERE studio_id=? AND id=?',(studio_id,media['job_id'])).fetchone()
            visible=bool(media['customer_visible'])
    elif len(relative.parts)==2 and relative.parts[0]=='dvi':
        job=conn.execute('''SELECT j.* FROM dvi_photos p JOIN dvi_inspections i ON i.id=p.inspection_id AND i.studio_id=p.studio_id
            JOIN jobs j ON j.id=i.job_id AND j.studio_id=i.studio_id WHERE p.studio_id=? AND p.filename=?''',(studio_id,relative.name)).fetchone()
    elif len(relative.parts)==2 and relative.parts[0]=='inspection':
        # Legacy panel arrays contain URLs. Require an exact reference, never a filename prefix guess.
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='panel_inspections'").fetchone():
            for panel in conn.execute('SELECT * FROM panel_inspections WHERE studio_id=?',(studio_id,)):
                values=[]
                for key in ('images','after_images'):
                    if key in panel.keys():
                        try:values+=json.loads(panel[key] or '[]')
                        except (ValueError,TypeError):pass
                if any(v in (f'/static/uploads/studio_{studio_id}/{filename}',f'/uploads/studio_{studio_id}/{filename}') for v in values):
                    job=conn.execute('SELECT * FROM jobs WHERE studio_id=? AND id=?',(studio_id,panel['job_id'])).fetchone();break
    authorize_job(job,visible)
    roots=[Path(Config.PRIVATE_UPLOAD_ROOT)]
    if not Config.PRODUCTION:roots.append(Path(Config.STATIC_FOLDER)/'uploads')
    for root in roots:
        studio_root=(root/f'studio_{studio_id}').resolve()
        path=studio_root/relative
        if path.is_symlink() or not path.resolve().is_relative_to(studio_root):abort(404)
        if path.is_file():
            response=send_file(path,conditional=True)
            response.headers['Cache-Control']='private, no-store'
            response.headers['X-Content-Type-Options']='nosniff'
            return response
    abort(404)


def install_security(app):
    @app.before_request
    def boundary():
        g.request_id=uuid.uuid4().hex
        if request.path.startswith('/customer/tracking/'):
            return '<h1>This tracking link has been replaced</h1><p><a href="/portal/">Open the customer portal</a> to sign in. If you do not have an account, ask your studio for an invitation.</p>',410
        if request.path.endswith('/send-tracking-sms'):
            return 'Legacy tracking messages are disabled. Use the reviewed follow-up workflow.',410
        if request.endpoint=='static':
            target=(Path(Config.STATIC_FOLDER)/(request.view_args or {}).get('filename','')).resolve()
            uploads=(Path(Config.STATIC_FOLDER)/'uploads').resolve()
            if target.is_relative_to(uploads):
                relative=target.relative_to(uploads).as_posix()
                private=re.fullmatch(r'studio_(\d+)/(.+)',relative)
                if not private:abort(404)
                return private_media(int(private[1]),private[2])
        match=re.fullmatch(r'/(?:static/)?uploads/studio_(\d+)/(.+)',request.path)
        if match:return private_media(int(match[1]),match[2])
        if request.endpoint=='dvi.serve_photo':
            return private_media(request.view_args['studio_id'],'dvi/'+request.view_args['filename'])
        if session.get('role')=='customer' and session.get('logged_in'):
            allowed=request.endpoint in ('main.login','main.logout','static','approvals.public') or str(request.endpoint).startswith('portal.')
            if not allowed:abort(403)
        # Existing media mutation paths now require both CSRF and job assignment.
        if request.method=='POST' and (request.path.startswith('/media/') or '/inspection-upload/' in request.path or re.fullmatch(r'/dvi/inspection/\d+/photos',request.path)):
            from .routes.job_record_routes import verify_csrf
            verify_csrf()
            jid=(request.view_args or {}).get('job_id') or request.form.get('job_id')
            if request.endpoint=='main.delete_media':
                media=get_db().execute('SELECT job_id FROM media WHERE studio_id=? AND id=?',(session.get('studio_id'),request.view_args['media_id'])).fetchone()
                jid=media['job_id'] if media else None
            if request.endpoint=='dvi.upload_photos':
                inspection=get_db().execute('SELECT job_id FROM dvi_inspections WHERE studio_id=? AND id=?',(session.get('studio_id'),request.view_args['inspection_id'])).fetchone()
                jid=inspection['job_id'] if inspection else None
            authorize_job(get_db().execute('SELECT * FROM jobs WHERE studio_id=? AND id=?',(session.get('studio_id'),jid)).fetchone())

    @app.after_request
    def headers(response):
        response.headers['X-Request-ID']=getattr(g,'request_id','')
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers.setdefault('Referrer-Policy','same-origin')
        response.headers.setdefault('X-Frame-Options','DENY')
        if session.get('logged_in'):response.headers['Cache-Control']='no-store'
        if Config.PRODUCTION:response.headers['Strict-Transport-Security']='max-age=31536000'
        return response

    @app.get('/healthz')
    def health():
        try:get_db().execute('SELECT 1').fetchone()
        except Exception:return {'status':'unavailable'},503
        return {'status':'ok'},200
