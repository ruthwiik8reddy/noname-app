"""Owner/manager-only intelligence. Every write requires a session-bound CSRF token."""
import secrets
import os
from flask import Blueprint, abort, jsonify, render_template, request, session
from ..auth import roles_required, auth_context
from ..services.orchestrators.business_intelligence import BusinessIntelligence
from ..services.intelligence.events import EventQueue
from ..services.repositories.agent_repository import AgentRepository

bp = Blueprint('intelligence', __name__, url_prefix='/intelligence')


def period(value):
    try:
        if isinstance(value, bool):
            raise ValueError()
        days = int(str(value))
        if not 7 <= days <= 90:
            raise ValueError()
        return days
    except (ValueError, TypeError):
        abort(400, description='Period must be an integer between 7 and 90 days.')


@bp.after_request
def private(response):
    response.headers['Cache-Control'] = 'no-store'
    return response


def verify_csrf():
    token = session.get('intelligence_csrf')
    if not token or not secrets.compare_digest(request.headers.get('X-CSRF-Token',''), token):
        abort(403)


@bp.get('/')
@roles_required('admin', 'general_manager')
def index():
    session.setdefault('intelligence_csrf', secrets.token_urlsafe(32))
    intelligence = BusinessIntelligence()
    return render_template('intelligence.html', **auth_context(), studio=session.get('studio'),
                           city=session.get('city'), owner=session.get('owner'), active_page='intelligence',
                           snapshot=intelligence.repo.snapshot(session['studio_id'], period(request.args.get('days',30))),
                           history=intelligence.history(session['studio_id']), csrf=session['intelligence_csrf'],
                           pending=EventQueue().status(session['studio_id']),
                           scheduler_enabled=os.getenv('AGENT_SCHEDULER','1').lower() not in ('0','false','no'))


@bp.get('/api/snapshot')
@roles_required('admin', 'general_manager')
def snapshot():
    return jsonify(BusinessIntelligence().repo.snapshot(session['studio_id'], period(request.args.get('days',30))))


@bp.post('/api/ask')
@roles_required('admin', 'general_manager')
def ask():
    verify_csrf()
    if request.content_length is not None and request.content_length > 8192:
        abort(413)
    payload = request.get_json(silent=True)
    if not isinstance(payload,dict):
        return jsonify(error='Send a JSON object with a question.'),400
    try:
        result = BusinessIntelligence().answer(session['studio_id'],payload.get('question'),
                                              session.get('name') or session.get('user_id'),period(payload.get('days',30)))
        return jsonify(result)
    except ValueError as exc:
        return jsonify(error=str(exc)),400


@bp.get('/api/reports/<int:report_id>')
@roles_required('admin', 'general_manager')
def report(report_id):
    result = BusinessIntelligence().report(session['studio_id'],report_id)
    if result is None:
        abort(404)
    return jsonify(result)


@bp.get('/api/health')
@roles_required('admin', 'general_manager')
def health():
    """Expose outcomes and pending work without backend URLs or credentials."""
    return jsonify(pending=EventQueue().status(session['studio_id']),
                   runs=AgentRepository().recent_runs(session['studio_id'],10),
                   scheduler_enabled=os.getenv('AGENT_SCHEDULER','1').lower() not in ('0','false','no'))


@bp.post('/api/retry-failed')
@roles_required('admin', 'general_manager')
def retry_failed():
    verify_csrf()
    return jsonify(retried=EventQueue().retry_failed(session['studio_id']))
