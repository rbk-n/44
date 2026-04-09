import json
from datetime import datetime
from flask import Blueprint, jsonify, request, session, render_template
from werkzeug.security import generate_password_hash
from app.auth import admin_required
from app.database import (get_db, get_user_by_username, get_user_by_email,
                           default_config, load_user_config)
from app.core.helpers import ytdlp_available, ffmpeg_available
from app.core.queue import jobs, job_queue

admin_bp = Blueprint('admin', __name__)


@admin_bp.route('/admin')
@admin_required
def admin_panel():
    user = {'id': session['user_id'], 'username': session['username'], 'role': 'admin'}
    return render_template('admin.html', user=user)


@admin_bp.route('/api/admin/stats')
@admin_required
def api_admin_stats():
    with get_db() as db:
        total_users   = db.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        active_users  = db.execute('SELECT COUNT(*) FROM users WHERE is_active=1').fetchone()[0]
        admin_count   = db.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
        total_history = db.execute('SELECT COUNT(*) FROM job_history').fetchone()[0]
        ok_history    = db.execute("SELECT COUNT(*) FROM job_history WHERE status='success'").fetchone()[0]
    active_jobs = sum(1 for j in jobs.values() if j.get('status') not in ('done', 'error'))
    return jsonify({
        'users':  {'total': total_users, 'active': active_users, 'admins': admin_count},
        'jobs':   {'total_history': total_history, 'success_history': ok_history,
                   'active_now': active_jobs, 'queue_size': len(job_queue)},
        'system': {'ytdlp': ytdlp_available(), 'ffmpeg': ffmpeg_available()}
    })


@admin_bp.route('/api/admin/users')
@admin_required
def api_admin_users():
    with get_db() as db:
        rows = db.execute(
            'SELECT id,username,email,role,is_active,created_at,last_login FROM users ORDER BY id'
        ).fetchall()
    result = []
    for row in rows:
        u = dict(row)
        u['job_count'] = sum(1 for j in jobs.values() if j.get('user_id') == u['id'])
        result.append(u)
    return jsonify(result)


@admin_bp.route('/api/admin/users/create', methods=['POST'])
@admin_required
def api_admin_create_user():
    data     = request.json or {}
    username = data.get('username', '').strip()
    email    = data.get('email', '').strip().lower()
    password = data.get('password', '').strip()
    role     = data.get('role', 'user')
    if not username or not email or not password:
        return jsonify({'error': 'All fields required'}), 400
    if role not in ('user', 'admin'):
        role = 'user'
    if get_user_by_username(username) or get_user_by_email(email):
        return jsonify({'error': 'Username or email already exists'}), 400
    with get_db() as db:
        cur = db.execute(
            'INSERT INTO users (username,email,password_hash,role,created_at) VALUES(?,?,?,?,?)',
            (username, email, generate_password_hash(password), role, datetime.now().isoformat())
        )
        new_id = cur.lastrowid
        db.execute('INSERT INTO user_settings (user_id,config_json) VALUES(?,?)',
                   (new_id, json.dumps(default_config())))
        db.commit()
    return jsonify({'ok': True, 'user_id': new_id})


@admin_bp.route('/api/admin/users/<int:uid>/toggle', methods=['POST'])
@admin_required
def api_admin_toggle_user(uid):
    if uid == session['user_id']:
        return jsonify({'error': 'Cannot deactivate yourself'}), 400
    with get_db() as db:
        row = db.execute('SELECT is_active FROM users WHERE id=?', (uid,)).fetchone()
        if not row:
            return jsonify({'error': 'User not found'}), 404
        new_val = 0 if row['is_active'] else 1
        db.execute('UPDATE users SET is_active=? WHERE id=?', (new_val, uid))
        db.commit()
    return jsonify({'ok': True, 'is_active': new_val})


@admin_bp.route('/api/admin/users/<int:uid>/role', methods=['POST'])
@admin_required
def api_admin_change_role(uid):
    if uid == session['user_id']:
        return jsonify({'error': 'Cannot change your own role'}), 400
    data = request.json or {}
    role = data.get('role', 'user')
    if role not in ('user', 'admin'):
        return jsonify({'error': 'Invalid role'}), 400
    with get_db() as db:
        db.execute('UPDATE users SET role=? WHERE id=?', (role, uid))
        db.commit()
    return jsonify({'ok': True})


@admin_bp.route('/api/admin/users/<int:uid>/reset_password', methods=['POST'])
@admin_required
def api_admin_reset_password(uid):
    data     = request.json or {}
    new_pass = data.get('password', '').strip()
    if len(new_pass) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
    with get_db() as db:
        db.execute('UPDATE users SET password_hash=? WHERE id=?',
                   (generate_password_hash(new_pass), uid))
        db.commit()
    return jsonify({'ok': True})


@admin_bp.route('/api/admin/users/<int:uid>', methods=['DELETE'])
@admin_required
def api_admin_delete_user(uid):
    if uid == session['user_id']:
        return jsonify({'error': 'Cannot delete yourself'}), 400
    with get_db() as db:
        db.execute('DELETE FROM user_settings WHERE user_id=?', (uid,))
        db.execute('DELETE FROM job_history WHERE user_id=?', (uid,))
        db.execute('DELETE FROM users WHERE id=?', (uid,))
        db.commit()
    return jsonify({'ok': True})


@admin_bp.route('/api/admin/jobs')
@admin_required
def api_admin_jobs():
    return jsonify([{
        'job_id':   jid,
        'user_id':  j.get('user_id'),
        'status':   j.get('status'),
        'progress': j.get('progress', 0),
        'stage':    j.get('stage', ''),
        'mode':     j.get('mode', 'transfer'),
        'title':    j.get('meta', {}).get('title', '—'),
        'error':    j.get('error', ''),
    } for jid, j in list(jobs.items())])


@admin_bp.route('/api/admin/history')
@admin_required
def api_admin_history():
    with get_db() as db:
        rows = db.execute(
            'SELECT jh.*,u.username FROM job_history jh'
            ' JOIN users u ON jh.user_id=u.id ORDER BY jh.id DESC LIMIT 200'
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@admin_bp.route('/api/admin/clear_jobs', methods=['POST'])
@admin_required
def api_admin_clear_jobs():
    to_rm = [jid for jid, j in list(jobs.items()) if j.get('status') in ('done', 'error')]
    for jid in to_rm:
        jobs.pop(jid, None)
    return jsonify({'ok': True, 'removed': len(to_rm)})
