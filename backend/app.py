import os
from datetime import datetime, timedelta, timezone
from functools import wraps

import psycopg2
from flask import Flask, flash, redirect, render_template, request, session, url_for
from psycopg2.extras import RealDictCursor

from rules import weigh

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "tea-cupping-dev-secret")

ACCOUNTS = {
    "taster": {"password": "tea123456", "role": "writer"},
    "observer": {"password": "look123456", "role": "reader"},
}

ACTION_OCCUPY = "占用"
ACTION_LEAVE = "离席"
ACTION_CLEAR = "清席"


def db():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def login_required(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrap


def seat_timeout_minutes(cur) -> int:
    cur.execute("SELECT value FROM app_settings WHERE key = 'seat_timeout_minutes'")
    row = cur.fetchone()
    return int(row["value"]) if row else 30


@app.get("/health")
def health():
    return {"status": "ok", "service": "tea-blend-cupping"}


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        name = request.form.get("username", "").strip()
        account = ACCOUNTS.get(name)
        if not account or account["password"] != request.form.get("password", ""):
            error = "用户名或密码错误"
        else:
            session["user"] = name
            session["role"] = account["role"]
            return redirect(url_for("home"))
    return render_template("login.html", error=error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def home():
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM cuppings ORDER BY id DESC")
        rows = cur.fetchall()
    return render_template("home.html", rows=rows, can_write=session.get("role") == "writer")


@app.post("/cuppings")
@login_required
def create():
    if session.get("role") != "writer":
        return ("仅审评员可提交拼配审评", 403)
    aroma = float(request.form["aroma"])
    taste = float(request.form["taste"])
    liquor = float(request.form["liquor"])
    lot = request.form["lot"].strip()
    verdict, note, score = weigh(aroma, taste, liquor)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT seat_id FROM seat_occupancies WHERE occupant = %s LIMIT 1",
            (session["user"],),
        )
        if cur.fetchone() is None:
            return ("未占用审评席，不能交评；请先到席位册占席", 403)
        cur.execute(
            """INSERT INTO cuppings (lot, aroma, taste, liquor, score, verdict, note, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (lot, aroma, taste, liquor, score, verdict, note, session["user"]),
        )
        row = cur.fetchone()
        conn.commit()
    if request.headers.get("HX-Request"):
        return render_template("_row.html", row=row)
    return redirect(url_for("home"))


@app.get("/seats")
@login_required
def seats():
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        timeout = seat_timeout_minutes(cur)
        cur.execute(
            """SELECT s.id, s.name, o.occupant, o.occupied_at
               FROM seats s
               LEFT JOIN seat_occupancies o ON o.seat_id = s.id
               ORDER BY s.id"""
        )
        seat_rows = cur.fetchall()
        cur.execute(
            """SELECT seat_name, actor, action, happened_at
               FROM seat_history ORDER BY happened_at, id"""
        )
        history = cur.fetchall()
    now = datetime.now(timezone.utc)
    for seat in seat_rows:
        occupied_at = seat["occupied_at"]
        seat["timed_out"] = bool(
            occupied_at and now >= occupied_at + timedelta(minutes=timeout)
        )
    return render_template(
        "seats.html",
        seats=seat_rows,
        history=history,
        timeout=timeout,
        can_write=session.get("role") == "writer",
    )


@app.post("/seats/register")
@login_required
def seat_register():
    if session.get("role") != "writer":
        return ("观察员不能登记席位", 403)
    name = request.form.get("name", "").strip()
    if not name:
        flash("席位名不能为空")
        return redirect(url_for("seats"))
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        try:
            cur.execute(
                "INSERT INTO seats (name, created_by) VALUES (%s, %s)",
                (name, session["user"]),
            )
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            flash("席位名已存在")
        else:
            conn.commit()
            flash(f"已登记席位 {name}")
    return redirect(url_for("seats"))


@app.post("/seats/<int:seat_id>/occupy")
@login_required
def seat_occupy(seat_id):
    if session.get("role") != "writer":
        return ("观察员不能占席", 403)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id, name FROM seats WHERE id = %s FOR UPDATE", (seat_id,))
        seat = cur.fetchone()
        if seat is None:
            return ("席位不存在", 404)
        try:
            cur.execute(
                "INSERT INTO seat_occupancies (seat_id, occupant) VALUES (%s, %s)",
                (seat_id, session["user"]),
            )
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            flash(f"{seat['name']} 已被占用")
        else:
            cur.execute(
                "INSERT INTO seat_history (seat_name, actor, action) VALUES (%s, %s, %s)",
                (seat["name"], session["user"], ACTION_OCCUPY),
            )
            conn.commit()
            flash(f"已占用 {seat['name']}")
    return redirect(url_for("seats"))


@app.post("/seats/<int:seat_id>/leave")
@login_required
def seat_leave(seat_id):
    if session.get("role") != "writer":
        return ("观察员不能离席", 403)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """SELECT o.id, o.occupant, s.name
               FROM seat_occupancies o JOIN seats s ON s.id = o.seat_id
               WHERE o.seat_id = %s FOR UPDATE""",
            (seat_id,),
        )
        occ = cur.fetchone()
        if occ is None:
            flash("该席当前无人占用")
        elif occ["occupant"] != session["user"]:
            flash("只能离开自己占用的席位")
        else:
            cur.execute("DELETE FROM seat_occupancies WHERE id = %s", (occ["id"],))
            cur.execute(
                "INSERT INTO seat_history (seat_name, actor, action) VALUES (%s, %s, %s)",
                (occ["name"], session["user"], ACTION_LEAVE),
            )
            flash(f"已离开 {occ['name']}")
        conn.commit()
    return redirect(url_for("seats"))


@app.post("/seats/<int:seat_id>/clear")
@login_required
def seat_clear(seat_id):
    if session.get("role") != "writer":
        return ("观察员不能清席", 403)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        timeout = seat_timeout_minutes(cur)
        cur.execute(
            """SELECT o.id, o.occupant, o.occupied_at, s.name
               FROM seat_occupancies o JOIN seats s ON s.id = o.seat_id
               WHERE o.seat_id = %s FOR UPDATE""",
            (seat_id,),
        )
        occ = cur.fetchone()
        if occ is None:
            flash("该席当前无人占用")
        elif datetime.now(timezone.utc) < occ["occupied_at"] + timedelta(minutes=timeout):
            flash("占用未超时，不能强制清席")
        else:
            cur.execute("DELETE FROM seat_occupancies WHERE id = %s", (occ["id"],))
            cur.execute(
                "INSERT INTO seat_history (seat_name, actor, action) VALUES (%s, %s, %s)",
                (occ["name"], session["user"], ACTION_CLEAR),
            )
            flash(f"已强制清席 {occ['name']}（原占用者 {occ['occupant']}）")
        conn.commit()
    return redirect(url_for("seats"))


@app.post("/seats/timeout")
@login_required
def seat_timeout():
    if session.get("role") != "writer":
        return ("观察员不能修改超时", 403)
    try:
        minutes = int(request.form.get("minutes", ""))
    except ValueError:
        flash("超时分钟必须是整数")
        return redirect(url_for("seats"))
    if minutes < 0:
        flash("超时分钟不能为负")
        return redirect(url_for("seats"))
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """INSERT INTO app_settings (key, value) VALUES ('seat_timeout_minutes', %s)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
            (str(minutes),),
        )
        conn.commit()
    flash(f"占席超时已改为 {minutes} 分钟，即刻生效")
    return redirect(url_for("seats"))
