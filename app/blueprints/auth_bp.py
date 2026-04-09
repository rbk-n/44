import re
from datetime import datetime
from flask import Blueprint, request, session, redirect, url_for, render_template
from werkzeug.security import generate_password_hash, check_password_hash
from app.database import get_db, get_user_by_username, get_user_by_email, default_config
import json

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('user_id'):
        return redirect(url_for('main.index'))
    error = None
    if request.method == 'POST':
        identifier = request.form.get('username', '').strip()
        password   = request.form.get('password', '')
        user = get_user_by_username(identifier) or get_user_by_email(identifier)
        if user and user['is_active'] and check_password_hash(user['password_hash'], password):
            session.permanent = True
            session['user_id']  = user['id']
            session['username'] = user['username']
            session['role']     = user['role']
            with get_db() as db:
                db.execute('UPDATE users SET last_login=? WHERE id=?',
                           (datetime.now().isoformat(), user['id']))
                db.commit()
            return redirect(url_for('main.index'))
        error = 'Неверный логин или пароль'
    return render_template('login.html', error=error, mode='login')


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if session.get('user_id'):
        return redirect(url_for('main.index'))
    error = None
    if request.method == 'POST':
        username  = request.form.get('username', '').strip()
        email     = request.form.get('email', '').strip().lower()
        password  = request.form.get('password', '')
        password2 = request.form.get('password2', '')
        if not username or not email or not password:
            error = 'Заполните все поля'
        elif len(username) < 3:
            error = 'Имя пользователя: минимум 3 символа'
        elif not re.match(r'^[a-zA-Z0-9_.-]+$', username):
            error = 'Имя пользователя: только латиница, цифры, _ . -'
        elif not re.match(r'^[^@]+@[^@]+\.[^@]+$', email):
            error = 'Некорректный email'
        elif len(password) < 6:
            error = 'Пароль: минимум 6 символов'
        elif password != password2:
            error = 'Пароли не совпадают'
        elif get_user_by_username(username):
            error = 'Имя пользователя уже занято'
        elif get_user_by_email(email):
            error = 'Email уже зарегистрирован'
        else:
            with get_db() as db:
                cur = db.execute(
                    "INSERT INTO users (username,email,password_hash,role,created_at) VALUES(?,?,?,'user',?)",
                    (username, email, generate_password_hash(password), datetime.now().isoformat())
                )
                new_id = cur.lastrowid
                db.execute('INSERT INTO user_settings (user_id,config_json) VALUES(?,?)',
                           (new_id, json.dumps(default_config())))
                db.commit()
            session.permanent = True
            session['user_id']  = new_id
            session['username'] = username
            session['role']     = 'user'
            return redirect(url_for('main.index'))
    return render_template('login.html', error=error, mode='register')


@auth_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))
