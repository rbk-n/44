from functools import wraps
from flask import session, redirect, url_for, request, jsonify
from app.database import get_user


def current_user() -> dict | None:
    uid = session.get("user_id")
    return get_user(uid) if uid else None


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("auth_bp.login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        uid = session.get("user_id")
        if not uid:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("auth_bp.login"))
        user = get_user(uid)
        if not user or user["role"] != "admin":
            if request.path.startswith("/api/"):
                return jsonify({"error": "Forbidden"}), 403
            return redirect(url_for("main_bp.index"))
        return f(*args, **kwargs)
    return decorated
