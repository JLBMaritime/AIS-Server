"""Login / logout / forced-password-change routes."""
from __future__ import annotations

from flask import (Blueprint, current_app, flash, redirect, render_template,
                   request, session, url_for)
from flask_login import (current_user, login_required, login_user, logout_user)

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("views.dashboard"))
    db = current_app.config["DB"]
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = db.verify_password(username, password)
        if user:
            login_user(user, remember=False)
            session.permanent = True
            if user.must_change_password and current_app.config[
                    "CFG"]["security"]["force_password_change_on_first_login"]:
                return redirect(url_for("auth.change_password"))
            return redirect(url_for("views.dashboard"))
        flash("Invalid username or password", "error")
    return render_template("login.html")


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


@bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    db = current_app.config["DB"]
    forced = current_user.must_change_password
    if request.method == "POST":
        current_pw = request.form.get("current_password") or ""
        new_pw = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""
        if not db.verify_password(current_user.username, current_pw):
            flash("Current password is incorrect", "error")
        elif len(new_pw) < 8:
            flash("New password must be at least 8 characters", "error")
        elif new_pw != confirm:
            flash("Passwords do not match", "error")
        else:
            db.set_password(current_user.username, new_pw,
                            clear_must_change=True)
            flash("Password updated", "success")
            return redirect(url_for("views.dashboard"))
    return render_template("change_password.html", forced=forced)
