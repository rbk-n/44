from flask import Blueprint, render_template, session, redirect, url_for
from app.auth import login_required, current_user

main_bp = Blueprint('main', __name__)


@main_bp.route('/')
@login_required
def index():
    user = current_user()
    return render_template('index.html', user=user)
