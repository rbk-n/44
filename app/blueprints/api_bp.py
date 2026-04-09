import json
import subprocess
import sys
from pathlib import Path
from flask import Blueprint, jsonify, request, session, send_from_directory
from app.auth import login_required
from app.database import (load_user_config, save_user_config, load_history,
                           get_db, get_user_by_username, get_user_by_email)
from app.core.helpers import ytdlp_available, ffmpeg_available, ytdlp_cmd, ytdlp_auth_args, safe_filename, is_valid_yt_url
from app.core.queue import jobs, job_queue, enqueue_job, enqueue_dub_job, enqueue_shorts_job
from app.core.dubbing import check_whisper, check_edge_tts
from app.services.telegram import tg_send
from app.services.monitor import extract_channel_id, fetch_rss_videos
from config import Config
from datetime import datetime

api_bp = Blueprint('api', __name__, url_prefix='/api')


@api_bp.route('/status')
@login_required
def api_status():
    uid = session['user_id']
    return jsonify({
        'ytdlp': ytdlp_available(),
        'ffmpeg': ffmpeg_available(),
        'config': load_user_config(uid)
    })


@api_bp.route('/start', methods=['POST'])
@login_required
def api_start():
    data = request.json or {}
    url  = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL'}), 400
    if not is_valid_yt_url(url):
        return jsonify({'error': 'Invalid YouTube URL'}), 400
    uid    = session['user_id']
    job_id = enqueue_job(url, load_user_config(uid), uid)
    return jsonify({'job_id': job_id})


@api_bp.route('/dub/start', methods=['POST'])
@login_required
def api_dub_start():
    data = request.json or {}
    url  = data.get('url', '').strip()
    if not url or not is_valid_yt_url(url):
        return jsonify({'error': 'Invalid YouTube URL'}), 400
    uid = session['user_id']
    cfg = load_user_config(uid)
    for k in ['dub_source_lang', 'dub_target_lang', 'dub_whisper_model', 'dub_mix_original']:
        if k in data:
            cfg[k] = data[k]
    return jsonify({'job_id': enqueue_dub_job(url, cfg, uid)})


@api_bp.route('/dub/status')
@login_required
def api_dub_status():
    return jsonify({'whisper': check_whisper(), 'edge_tts': check_edge_tts(), 'ffmpeg': ffmpeg_available()})


@api_bp.route('/dub/install', methods=['POST'])
@login_required
def api_dub_install():
    results = {}
    for pkg in ['openai-whisper', 'edge-tts', 'deep-translator']:
        r = subprocess.run([sys.executable, '-m', 'pip', 'install', pkg, '--break-system-packages', '-q'],
                           capture_output=True, text=True, timeout=600)
        results[pkg] = r.returncode == 0
    return jsonify({'ok': all(results.values()), 'results': results})


@api_bp.route('/dub/download/<job_id>')
@login_required
def api_dub_download(job_id):
    j = jobs.get(job_id)
    if not j or not j.get('dubbed_file'):
        return jsonify({'error': 'Not found'}), 404
    if j.get('user_id') != session['user_id'] and session.get('role') != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    p = Path(j['dubbed_file'])
    if not p.exists():
        return jsonify({'error': 'File not found'}), 404
    return send_from_directory(str(p.parent), p.name, as_attachment=True)


@api_bp.route('/shorts/start', methods=['POST'])
@login_required
def api_shorts_start():
    data = request.json or {}
    url  = data.get('url', '').strip()
    if not url or not is_valid_yt_url(url):
        return jsonify({'error': 'Invalid YouTube URL'}), 400
    uid = session['user_id']
    cfg = load_user_config(uid)
    for k in ['shorts_count', 'shorts_duration', 'shorts_strategy']:
        if k in data:
            cfg[k] = data[k]
    return jsonify({'job_id': enqueue_shorts_job(url, cfg, uid)})


@api_bp.route('/shorts/<job_id>')
@login_required
def api_shorts_list(job_id):
    j = jobs.get(job_id)
    if not j:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'shorts': j.get('shorts', [])})


@api_bp.route('/shorts/download/<filename>')
@login_required
def api_shorts_download(filename):
    filename = safe_filename(filename, 'clip.mp4')
    return send_from_directory(str(Config.SHORTS_DIR), filename, as_attachment=True)


@api_bp.route('/job/<job_id>')
@login_required
def api_job(job_id):
    j = jobs.get(job_id)
    if not j:
        return jsonify({'error': 'Not found'}), 404
    if j.get('user_id') != session['user_id'] and session.get('role') != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    return jsonify(j)


@api_bp.route('/history', methods=['GET', 'DELETE'])
@login_required
def api_history():
    uid = session['user_id']
    if request.method == 'DELETE':
        with get_db() as db:
            db.execute('DELETE FROM job_history WHERE user_id=?', (uid,))
            db.commit()
        return jsonify({'ok': True})
    page     = int(request.args.get('page', 1))
    per_page = 20
    search   = request.args.get('search', '').strip()
    with get_db() as db:
        if search:
            rows = db.execute(
                'SELECT * FROM job_history WHERE user_id=? AND (title LIKE ? OR youtube_url LIKE ?)'
                ' ORDER BY id DESC LIMIT ? OFFSET ?',
                (uid, f'%{search}%', f'%{search}%', per_page, (page-1)*per_page)
            ).fetchall()
            total = db.execute(
                'SELECT COUNT(*) FROM job_history WHERE user_id=? AND (title LIKE ? OR youtube_url LIKE ?)',
                (uid, f'%{search}%', f'%{search}%')
            ).fetchone()[0]
        else:
            rows  = db.execute('SELECT * FROM job_history WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?',
                               (uid, per_page, (page-1)*per_page)).fetchall()
            total = db.execute('SELECT COUNT(*) FROM job_history WHERE user_id=?', (uid,)).fetchone()[0]
    return jsonify({'history': [dict(r) for r in rows], 'total': total, 'page': page, 'per_page': per_page})


@api_bp.route('/settings', methods=['GET', 'POST'])
@login_required
def api_settings():
    uid = session['user_id']
    if request.method == 'POST':
        data = request.json or {}
        cfg  = load_user_config(uid)
        cfg.update({k: v for k, v in data.items() if k in cfg or k in (
            'yt_cookies', 'rutube_cookies', 'yt_cookie_file', 'rutube_cookie_file',
            'tg_token', 'tg_chat_id', 'tg_enabled', 'quality', 'proxy', 'keep_files'
        )})
        # map frontend keys to config keys
        key_map = {'tg_token': 'telegram_bot_token', 'tg_chat_id': 'telegram_chat_id',
                   'tg_enabled': 'telegram_enabled'}
        for fk, ck in key_map.items():
            if fk in data:
                cfg[ck] = data[fk]
        if 'yt_cookies' in data:
            _save_cookie_file(cfg.get('yt_cookie_file', 'yt_cookies.txt'), data['yt_cookies'])
        if 'rutube_cookies' in data:
            _save_cookie_file(cfg.get('rutube_cookie_file', 'rutube_cookies.txt'), data['rutube_cookies'])
        save_user_config(uid, cfg)
        return jsonify({'ok': True})
    cfg = load_user_config(uid)
    # read cookie file contents
    result = dict(cfg)
    result['yt_cookies']     = _read_cookie_file(cfg.get('yt_cookie_file', 'yt_cookies.txt'))
    result['rutube_cookies'] = _read_cookie_file(cfg.get('rutube_cookie_file', 'rutube_cookies.txt'))
    result['tg_token']       = cfg.get('telegram_bot_token', '')
    result['tg_chat_id']     = cfg.get('telegram_chat_id', '')
    result['tg_enabled']     = cfg.get('telegram_enabled', False)
    return jsonify(result)


def _save_cookie_file(fname, content):
    try:
        fname = safe_filename(fname, 'cookies.txt')
        Path(fname).write_text(content, encoding='utf-8')
    except Exception:
        pass


def _read_cookie_file(fname):
    try:
        fname = safe_filename(fname, 'cookies.txt')
        p = Path(fname)
        return p.read_text(encoding='utf-8', errors='ignore') if p.exists() else ''
    except Exception:
        return ''


@api_bp.route('/preview', methods=['POST'])
@login_required
def api_preview():
    data = request.json or {}
    url  = data.get('url', '').strip()
    if not url or not is_valid_yt_url(url):
        return jsonify({'error': 'Invalid YouTube URL'}), 400
    try:
        cfg = load_user_config(session['user_id'])
        cmd = ytdlp_cmd() + ['--dump-json', '--no-playlist', '--no-warnings'] + ytdlp_auth_args(cfg) + [url]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if res.returncode != 0:
            return jsonify({'error': 'Could not fetch metadata'}), 400
        meta = json.loads(res.stdout)
        return jsonify({
            'title':       meta.get('title', 'Untitled')[:120],
            'channel':     meta.get('uploader', ''),
            'duration':    meta.get('duration', 0),
            'view_count':  meta.get('view_count', 0),
            'like_count':  meta.get('like_count', 0),
            'tags':        meta.get('tags', [])[:20],
            'description': meta.get('description', '')[:300],
            'thumbnail':   meta.get('thumbnail', ''),
            'upload_date': meta.get('upload_date', ''),
        })
    except Exception as e:
        return jsonify({'error': str(e)[:200]}), 500


@api_bp.route('/telegram/test', methods=['POST'])
@login_required
def api_telegram_test():
    cfg     = load_user_config(session['user_id'])
    token   = cfg.get('telegram_bot_token', '').strip()
    chat_id = cfg.get('telegram_chat_id', '').strip()
    if not token or not chat_id:
        return jsonify({'ok': False, 'error': 'Не указан токен или chat_id'}), 400
    result = tg_send(token, chat_id, '✅ <b>YT→Rutube</b>\n\nТест уведомлений работает!')
    if result and result.get('ok'):
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': str(result)}), 400


@api_bp.route('/monitor', methods=['GET'])
@login_required
def api_monitor_get():
    cfg = load_user_config(session['user_id'])
    return jsonify({'channels': cfg.get('monitor_channels', []),
                    'enabled': cfg.get('monitor_enabled', False),
                    'interval': cfg.get('monitor_interval', 15)})


@api_bp.route('/monitor', methods=['POST'])
@login_required
def api_monitor_add():
    data = request.json or {}
    url  = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL'}), 400
    uid        = session['user_id']
    cfg        = load_user_config(uid)
    channels   = cfg.get('monitor_channels', [])
    channel_id = extract_channel_id(url)
    existing   = {c.get('channel_id', '') for c in channels if isinstance(c, dict)}
    if channel_id in existing:
        return jsonify({'ok': False, 'error': 'Канал уже отслеживается'})
    entry = {'url': url, 'channel_id': channel_id, 'name': url,
             'added': datetime.now().strftime('%Y-%m-%d %H:%M')}
    channels.append(entry)
    cfg['monitor_channels'] = channels
    save_user_config(uid, cfg)
    state = json.loads(Config.MONITOR_STATE_FILE.read_text()) if Config.MONITOR_STATE_FILE.exists() else {}
    if channel_id not in state:
        videos = fetch_rss_videos(channel_id)
        state[channel_id] = [v['video_id'] for v in videos]
        Config.MONITOR_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    return jsonify({'ok': True, 'channel': entry})


@api_bp.route('/monitor/<channel_id>', methods=['DELETE'])
@login_required
def api_monitor_remove(channel_id):
    uid = session['user_id']
    cfg = load_user_config(uid)
    cfg['monitor_channels'] = [
        c for c in cfg.get('monitor_channels', [])
        if (c.get('channel_id') if isinstance(c, dict) else c) != channel_id
    ]
    save_user_config(uid, cfg)
    return jsonify({'ok': True})


@api_bp.route('/monitor/toggle', methods=['POST'])
@login_required
def api_monitor_toggle():
    data = request.json or {}
    uid  = session['user_id']
    cfg  = load_user_config(uid)
    cfg['monitor_enabled']  = bool(data.get('enabled', False))
    if 'interval' in data:
        cfg['monitor_interval'] = max(5, int(data['interval']))
    save_user_config(uid, cfg)
    return jsonify({'ok': True, 'enabled': cfg['monitor_enabled']})


@api_bp.route('/batch', methods=['POST'])
@login_required
def api_batch():
    data    = request.json or {}
    urls    = [u.strip() for u in data.get('urls', '').split('\n') if u.strip()]
    ids, invalid = [], []
    uid     = session['user_id']
    cfg     = load_user_config(uid)
    for url in urls:
        if not is_valid_yt_url(url):
            invalid.append(url)
            continue
        ids.append({'job_id': enqueue_job(url, cfg, uid), 'url': url})
    return jsonify({'jobs': ids, 'invalid': invalid, 'total': len(urls)})


@api_bp.route('/dashboard')
@login_required
def api_dashboard():
    uid  = session['user_id']
    role = session.get('role')
    active, recent = [], []
    stats = {'total': 0, 'done': 0, 'error': 0, 'running': 0, 'queued': 0}
    for jid, j in list(jobs.items()):
        if j.get('user_id') != uid and role != 'admin':
            continue
        stats['total'] += 1
        s = j.get('status', 'unknown')
        if s in stats:
            stats[s] += 1
        entry = {'job_id': jid, 'status': s, 'progress': j.get('progress', 0),
                 'stage': j.get('stage', ''), 'meta': j.get('meta', {}),
                 'mode': j.get('mode', 'transfer'), 'shorts_count': len(j.get('shorts', []))}
        if s in ('running', 'starting', 'queued'):
            active.append(entry)
        else:
            recent.append(entry)
    stats['queue_size'] = len(job_queue)
    return jsonify({'active': active, 'recent': recent[-20:], 'stats': stats})
