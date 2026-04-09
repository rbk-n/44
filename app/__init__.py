from flask import Flask
from config import Config

try:
    from flask_sock import Sock
    sock = Sock()
    _ws_available = True
except ImportError:
    sock = None
    _ws_available = False


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    app.secret_key = Config.SECRET_KEY
    import datetime
    app.permanent_session_lifetime = datetime.timedelta(seconds=Config.PERMANENT_SESSION_LIFETIME)

    from app.database import init_db
    init_db()

    if _ws_available and sock:
        sock.init_app(app)
        app.extensions['sock'] = sock
        app.extensions['ws_available'] = True
    else:
        app.extensions['ws_available'] = False

    from app.blueprints.auth_bp import auth_bp
    from app.blueprints.main_bp import main_bp
    from app.blueprints.api_bp import api_bp
    from app.blueprints.admin_bp import admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(admin_bp)

    from app.services.monitor import start_monitor
    start_monitor()

    # WebSocket handler
    if _ws_available and sock:
        import threading
        from app.core.queue import ws_clients, ws_lock

        @sock.route('/ws')
        def ws_handler(ws):
            from flask import session as flask_session
            with ws_lock:
                ws_clients.add(ws)
            try:
                import json
                while True:
                    data = ws.receive(timeout=30)
                    if data is None:
                        break
                    try:
                        msg = json.loads(data)
                        if msg.get('type') == 'ping':
                            ws.send(json.dumps({'type': 'pong'}))
                    except Exception:
                        pass
            except Exception:
                pass
            finally:
                with ws_lock:
                    ws_clients.discard(ws)

    return app
