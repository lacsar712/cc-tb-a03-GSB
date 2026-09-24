import os
from functools import wraps

import psycopg2
from flask import Flask, redirect, render_template, request, session, url_for
from psycopg2.extras import RealDictCursor

from rules import weigh

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "tea-cupping-dev-secret")

ACCOUNTS = {
    "taster": {"password": "tea123456", "role": "writer"},
    "observer": {"password": "look123456", "role": "reader"},
}

DEFAULT_SEAT_TIMEOUT_MINUTES = 30


def db():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def login_required(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrap


def get_seat_timeout(cur):
    cur.execute("SELECT value FROM settings WHERE key = 'seat_timeout_minutes'")
    row = cur.fetchone()
    return int(row["value"]) if row else DEFAULT_SEAT_TIMEOUT_MINUTES


def render_seats(error=None, status=200):
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        timeout = get_seat_timeout(cur)
        cur.execute(
            """SELECT s.id, s.name, s.created_by, o.occupant, o.occupied_at,
                      (o.occupied_at IS NOT NULL
                       AND o.occupied_at + make_interval(mins => %s) <= now()) AS expired
               FROM seats s
               LEFT JOIN seat_occupancy o ON o.seat_id = s.id
               ORDER BY s.id""",
            (timeout,),
        )
        seat_rows = cur.fetchall()
        cur.execute(
            """SELECT e.id, s.name AS seat_name, e.actor, e.action, e.created_at
               FROM seat_events e JOIN seats s ON s.id = e.seat_id
               ORDER BY e.id DESC LIMIT 200"""
        )
        events = cur.fetchall()
    return (
        render_template(
            "seats.html",
            seats=seat_rows,
            events=events,
            timeout=timeout,
            can_write=session.get("role") == "writer",
            error=error,
        ),
        status,
    )


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
            "SELECT 1 FROM seat_occupancy WHERE occupant = %s LIMIT 1",
            (session["user"],),
        )
        if cur.fetchone() is None:
            return ("未占用审评席，不能交评，请先到席位页占席", 403)
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
    return render_seats()


@app.post("/seats")
@login_required
def register_seat():
    if session.get("role") != "writer":
        return ("观察员不能登记席位", 403)
    name = request.form.get("name", "").strip()
    if not name:
        return render_seats("席位名不能为空", 400)
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO seats (name, created_by) VALUES (%s, %s)",
                (name, session["user"]),
            )
            conn.commit()
    except psycopg2.errors.UniqueViolation:
        return render_seats("席位名已存在", 409)
    return redirect(url_for("seats"))


@app.post("/seats/<int:seat_id>/occupy")
@login_required
def occupy_seat(seat_id):
    if session.get("role") != "writer":
        return ("观察员不能占席", 403)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id FROM seats WHERE id = %s", (seat_id,))
        if cur.fetchone() is None:
            return ("席位不存在", 404)
        timeout = get_seat_timeout(cur)
        cur.execute(
            """INSERT INTO seat_occupancy (seat_id, occupant, occupied_at)
               VALUES (%s, %s, now())
               ON CONFLICT (seat_id) DO UPDATE
               SET occupant = EXCLUDED.occupant, occupied_at = now()
               WHERE seat_occupancy.occupied_at + make_interval(mins => %s) <= now()""",
            (seat_id, session["user"], timeout),
        )
        if cur.rowcount == 0:
            conn.rollback()
            return render_seats("该席位已被占用，未超时前不能再占", 409)
        cur.execute(
            "INSERT INTO seat_events (seat_id, actor, action) VALUES (%s, %s, '占用')",
            (seat_id, session["user"]),
        )
        conn.commit()
    return redirect(url_for("seats"))


@app.post("/seats/<int:seat_id>/leave")
@login_required
def leave_seat(seat_id):
    if session.get("role") != "writer":
        return ("观察员不能离席", 403)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT occupant FROM seat_occupancy WHERE seat_id = %s", (seat_id,))
        row = cur.fetchone()
        if row is None:
            return render_seats("该席位当前空闲，无需离席", 409)
        if row["occupant"] != session["user"]:
            return render_seats("只能离开自己占用的席位", 409)
        cur.execute(
            "DELETE FROM seat_occupancy WHERE seat_id = %s AND occupant = %s",
            (seat_id, session["user"]),
        )
        cur.execute(
            "INSERT INTO seat_events (seat_id, actor, action) VALUES (%s, %s, '离席')",
            (seat_id, session["user"]),
        )
        conn.commit()
    return redirect(url_for("seats"))


@app.post("/seats/<int:seat_id>/clear")
@login_required
def clear_seat(seat_id):
    if session.get("role") != "writer":
        return ("观察员不能清席", 403)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        timeout = get_seat_timeout(cur)
        cur.execute(
            """DELETE FROM seat_occupancy
               WHERE seat_id = %s
                 AND occupied_at + make_interval(mins => %s) <= now()""",
            (seat_id, timeout),
        )
        if cur.rowcount == 0:
            conn.rollback()
            cur.execute("SELECT occupant FROM seat_occupancy WHERE seat_id = %s", (seat_id,))
            if cur.fetchone() is not None:
                return render_seats("占用未超时，不能强制清席", 409)
            return render_seats("该席位当前空闲，无需清席", 409)
        cur.execute(
            "INSERT INTO seat_events (seat_id, actor, action) VALUES (%s, %s, '清席')",
            (seat_id, session["user"]),
        )
        conn.commit()
    return redirect(url_for("seats"))


@app.post("/seats/timeout")
@login_required
def set_seat_timeout():
    if session.get("role") != "writer":
        return ("观察员不能修改超时分钟", 403)
    raw = request.form.get("minutes", "").strip()
    try:
        minutes = int(raw)
    except ValueError:
        return render_seats("超时分钟必须是不小于 0 的整数", 400)
    if minutes < 0:
        return render_seats("超时分钟必须是不小于 0 的整数", 400)
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO settings (key, value) VALUES ('seat_timeout_minutes', %s)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
            (str(minutes),),
        )
        conn.commit()
    return redirect(url_for("seats"))
