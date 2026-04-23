"""Server-side rendered pages (the HTML shell – data comes via /api + SocketIO)."""
from __future__ import annotations

from flask import Blueprint, current_app, redirect, render_template, url_for
from flask_login import current_user, login_required

bp = Blueprint("views", __name__)


def _guard_forced_change():
    """Redirect to the change-password page if the user hasn't changed it yet."""
    if current_user.is_authenticated and current_user.must_change_password \
       and current_app.config["CFG"]["security"][
           "force_password_change_on_first_login"]:
        return redirect(url_for("auth.change_password"))
    return None


@bp.route("/")
@login_required
def dashboard():
    r = _guard_forced_change()
    return r or render_template("dashboard.html")


@bp.route("/wifi")
@login_required
def wifi():
    r = _guard_forced_change()
    return r or render_template("wifi.html")


@bp.route("/nodes")
@login_required
def nodes():
    r = _guard_forced_change()
    return r or render_template("nodes.html")


@bp.route("/data/in")
@login_required
def data_in():
    r = _guard_forced_change()
    return r or render_template("data_in.html")


@bp.route("/data/out")
@login_required
def data_out():
    r = _guard_forced_change()
    return r or render_template("data_out.html")


@bp.route("/endpoints")
@login_required
def endpoints():
    r = _guard_forced_change()
    return r or render_template("endpoints.html")


@bp.route("/system")
@login_required
def system():
    r = _guard_forced_change()
    return r or render_template("system.html")
