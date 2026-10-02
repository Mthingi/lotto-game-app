from dotenv import load_dotenv
import os

load_dotenv(override=True)

from flask import Flask, request, jsonify, render_template, Response, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
import pyotp
import uuid
import random
import json
from datetime import datetime, timedelta
import http.client
import urllib.parse
import ssl

import africastalking

from resultsza import get_firestore_result

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from worker import process_ticket, init_worker

# =========================================
# FIREBASE
# =========================================
try:

    cred = credentials.Certificate("firebase.json")

    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)

    db = firestore.client()

    tickets_ref = db.collection("tickets")
    users_ref = db.collection("users")

    def fix_payment_status():

        tickets = tickets_ref.stream()


    print("✅ Firebase connected")

except Exception as e:

    db = None
    tickets_ref = None

    print("❌ Firebase error:", e)

# =========================================
# AFRICAS TALKING
# =========================================
AT_USERNAME = os.getenv("AT_USERNAME")
AT_API_KEY = os.getenv("AT_API_KEY")

print("Backend Username:", repr(AT_USERNAME))

sms = None

try:

    if AT_USERNAME and AT_API_KEY:

        africastalking.initialize(
            AT_USERNAME,
            AT_API_KEY
        )

        sms = africastalking.SMS

        print("=" * 50)
        print("Africa's Talking Configuration")
        print("Username :", AT_USERNAME)
        print("API Key  :", AT_API_KEY[:12] + "...")
        print("SMS Obj  :", sms)
        print("=" * 50)

        print("✅ SMS initialized")

    else:

        print("⚠️ SMS disabled")

except Exception as e:

    print("❌ Africa's Talking error:", e)

# =========================================
# AFRICAS TALKING SMS - DIRECT HTTPS
# =========================================

def send_sms_direct(phone, message):
    """
    Send SMS directly to Africa's Talking using HTTPS.
    This bypasses the Python SDK's requests/urllib3 SSL issue.
    """

    if not AT_API_KEY:
        raise RuntimeError("AT_API_KEY is not configured")

    if not AT_USERNAME:
        raise RuntimeError("AT_USERNAME is not configured")

    # Africa's Talking sandbox requires username=sandbox
    username = AT_USERNAME

    payload = urllib.parse.urlencode({
        "username": username,
        "to": phone,
        "message": message
    })

    headers = {
        "apiKey": AT_API_KEY,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json"
    }

    context = ssl.create_default_context()

    conn = http.client.HTTPSConnection(
        "api.sandbox.africastalking.com"
        if username == "sandbox"
        else "api.africastalking.com",
        443,
        context=context,
        timeout=20
    )

    try:

        conn.request(
            "POST",
            "/version1/messaging",
            body=payload,
            headers=headers
        )

        response = conn.getresponse()

        status_code = response.status
        response_body = response.read().decode("utf-8", errors="replace")

        print("=" * 60)
        print("AFRICA'S TALKING DIRECT SMS")
        print("Phone:", phone)
        print("HTTP Status:", status_code)
        print("Response:", response_body)
        print("=" * 60)

        if not 200 <= status_code < 300:
            raise RuntimeError(
                f"Africa's Talking SMS failed: "
                f"HTTP {status_code} - {response_body}"
            )

        return {
            "status_code": status_code,
            "response": response_body
        }

    finally:
        conn.close()

# =========================================
# INIT WORKER
# =========================================
init_worker(tickets_ref, send_sms_direct)

# =========================================
# FLASK
# =========================================
app = Flask(__name__)
app.secret_key = os.getenv('MPG_SECRET_KEY')

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("MPG_ENV") == "production"
)

# =========================================
# RESPONSE HELPER
# =========================================
def ussd_response(message):

    response = Response(
        message,
        status=200
    )

    response.headers[
        "Content-Type"
    ] = "text/plain; charset=utf-8"

    return response

# =====================================
# =========================================
# AUTHENTICATION & ROLE ACCESS
# =========================================

from functools import wraps

ROLE_PERMISSIONS = {
    "OWNER": [
        "admin",
        "results",
        "finance",
        "support",
        "sms",
        "maintenance"
    ],
    "RESULTS": [
        "results"
    ],
    "FINANCE": [
        "finance"
    ],
    "SUPPORT": [
        "support"
    ],
    "SMS": [
        "sms"
    ]
}


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):

        if "user_id" not in session:
            return redirect(url_for("login"))

        return view(*args, **kwargs)

    return wrapped_view


def role_required(permission):
    def decorator(view):
        @wraps(view)
        def wrapped_view(*args, **kwargs):

            if "user_id" not in session:
                return redirect(url_for("login"))

            role = session.get("role")

            if role not in ROLE_PERMISSIONS:
                session.clear()
                return redirect(url_for("login"))

            if permission not in ROLE_PERMISSIONS[role]:
                return "Access denied", 403

            return view(*args, **kwargs)

        return wrapped_view

    return decorator


def verify_totp(user, code):
    secret = user.get("totp_secret")

    if not secret:
        return False

    totp = pyotp.TOTP(secret)

    return totp.verify(code, valid_window=1)

def generate_totp_secret():
    return pyotp.random_base32()


@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        username = request.form.get(
            "username",
            ""
        ).strip()

        password = request.form.get(
            "password",
            ""
        )

        user_doc = users_ref.document(
            username
        ).get()

        if not user_doc.exists:

            return render_template(
                "login.html",
                error="Invalid username or password"
            )

        user = user_doc.to_dict()

        password_hash = user.get(
            "password_hash"
        )

        if not password_hash or not check_password_hash(
            password_hash,
            password
        ):

            return render_template(
                "login.html",
                error="Invalid username or password"
            )

        if not user.get("active", True):

            print("LOGIN BLOCKED: ACCOUNT INACTIVE", username)

            return render_template(
                "login.html",
                error="This account is inactive"
            )

        if user.get("role") == "OWNER" and user.get("totp_enabled"):

            session.clear()

            session["pending_2fa_user"] = username

            return redirect(
                url_for("login_2fa")
            )


        session.clear()

        session["user_id"] = username
        session["role"] = user.get(
            "role",
            "SUPPORT"
        )

        session["name"] = user.get(
            "name",
            username
        )

        landing_pages = {
            "OWNER": "admin",
            "RESULTS": "draws",
            "FINANCE": "reports",
            "SUPPORT": "tickets",
            "SMS": "sms_management"
        }

        return redirect(
            url_for(landing_pages.get(session["role"], "admin"))
        )

    return render_template("login.html")

@app.route("/login/2fa", methods=["GET", "POST"])
def login_2fa():

    if "pending_2fa_user" not in session:
        return redirect(url_for("login"))

    username = session.get("pending_2fa_user")

    user_doc = users_ref.document(username).get()

    if not user_doc.exists:
        session.clear()
        return redirect(url_for("login"))

    user = user_doc.to_dict()

    if request.method == "POST":

        code = request.form.get(
            "code",
            ""
        ).strip()

        if not verify_totp(user, code):

            return render_template(
                "login_2fa.html",
                error="Invalid authentication code. Please try again."
            )

        session.clear()

        session["user_id"] = username
        session["role"] = user.get(
            "role",
            "SUPPORT"
        )

        session["name"] = user.get(
            "name",
            username
        )

        landing_pages = {
            "OWNER": "admin",
            "RESULTS": "draws",
            "FINANCE": "reports",
            "SUPPORT": "tickets",
            "SMS": "sms_management"
        }

        return redirect(
            url_for(
                landing_pages.get(
                    session["role"],
                    "admin"
                )
            )
        )

    return render_template(
        "login_2fa.html"
    )


@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("login")
    )

@app.route("/owner/2fa/setup", methods=["GET", "POST"])
@role_required("admin")
def owner_2fa_setup():

    username = session.get("user_id")

    user_ref = users_ref.document(username)
    user_doc = user_ref.get()

    if not user_doc.exists:
        session.clear()
        return redirect(url_for("login"))

    user = user_doc.to_dict()

    if user.get("role") != "OWNER":
        return "Access denied", 403

    secret = user.get("totp_secret")

    if not secret:
        secret = generate_totp_secret()

        user_ref.update({
            "totp_secret": secret,
            "totp_enabled": False
        })

        user["totp_secret"] = secret

    if request.method == "POST":

        code = request.form.get(
            "code",
            ""
        ).strip()

        if not verify_totp(user, code):

            return render_template(
                "owner_2fa_setup.html",
                secret=secret,
                error="Invalid authentication code. Please try again."
            )

        user_ref.update({
            "totp_enabled": True
        })

        return redirect(url_for("admin"))

    return render_template(
        "owner_2fa_setup.html",
        secret=secret
    )

# =========================================
# HEALTH CHECK
# =========================================
@app.route("/", methods=["GET"])
def home():

    return render_template("login.html")


@app.route("/admin")
@role_required("admin")
def admin():

    docs = tickets_ref.stream()

    total = 0
    revenue = 0
    winners = 0
    losers = 0
    pending = 0
    prize_total = 0

    lotto = 0
    powerball = 0
    daily = 0

    tickets = []

    for doc in docs:

        if not doc.id.startswith("MPG"):
            continue

        data = doc.to_dict()

        if data.get("is_test"):
            continue

        data["reference"] = doc.id

        data.setdefault("game", "")
        data.setdefault("won", False)
        data.setdefault("result_checked", False)
        data.setdefault("winnings", 0)
        data.setdefault("cost", 0)

        total += 1
        revenue += data["cost"]

        if not data["result_checked"]:
            pending += 1

        elif data["won"]:
            winners += 1
            prize_total += data["winnings"]

        else:
            losers += 1

        game = data["game"]

        if game == "Lotto" or game == "Lotto Plus":
            lotto += 1

        elif game == "PowerBall" or game == "PowerBall Plus":
            powerball += 1

        elif game == "Daily Lotto":
            daily += 1

        tickets.append(data)

    tickets.reverse()

    recent_tickets = tickets[:10]

    return render_template(
        "admin.html",
        total=total,
        revenue=revenue,
        winners=winners,
        losers=losers,
        pending=pending,
        prize_total=prize_total,
        lotto=lotto,
        powerball=powerball,
        daily=daily,
        tickets=recent_tickets,
        )


@app.route("/user-management")
@role_required("admin")
def user_management():

    users = []

    for doc in users_ref.stream():

        data = doc.to_dict()

        users.append({
            "username": doc.id,
            "name": data.get("name", ""),
            "role": data.get("role", ""),
            "active": data.get("active", True)
        })

    users.sort(key=lambda user: user["username"].lower())

    return render_template(
        "user_management.html",
        users=users
    )


@app.route("/user-management/create", methods=["POST"])
@role_required("admin")
def create_user():

    username = request.form.get("username", "").strip().lower()
    name = request.form.get("name", "").strip()
    role = request.form.get("role", "").strip().upper()
    password = request.form.get("password", "")

    allowed_roles = {
        "RESULTS",
        "FINANCE",
        "SUPPORT",
        "SMS"
    }

    if not username or not name or not role or not password:
        return "All fields are required", 400

    if role not in allowed_roles:
        return "Invalid user role", 400

    if users_ref.document(username).get().exists:
        return "Username already exists", 400

    if len(password) < 8:
        return "Password must be at least 8 characters", 400

    users_ref.document(username).set({
        "name": name,
        "role": role,
        "active": True,
        "password_hash": generate_password_hash(password)
    })

    return redirect(url_for("user_management"))


@app.route("/user-management/edit/<username>", methods=["GET", "POST"])
@role_required("admin")
def edit_user(username):

    username = username.strip().lower()

    user_ref = users_ref.document(username)
    user_doc = user_ref.get()

    if not user_doc.exists:
        return "User not found", 404

    data = user_doc.to_dict()

    # The OWNER account cannot be edited here.
    if data.get("role") == "OWNER":
        return "The OWNER account cannot be edited here", 403

    allowed_roles = {
        "RESULTS",
        "FINANCE",
        "SUPPORT",
        "SMS"
    }

    if request.method == "POST":

        name = request.form.get("name", "").strip()
        role = request.form.get("role", "").strip().upper()

        if not name:
            return "Name is required", 400

        if role not in allowed_roles:
            return "Invalid user role", 400

        user_ref.update({
            "name": name,
            "role": role
        })

        return redirect(url_for("user_management"))

    return render_template(
        "edit_user.html",
        username=username,
        name=data.get("name", ""),
        role=data.get("role", "SUPPORT")
    )

@app.route("/user-management/toggle/<username>", methods=["POST"])
@role_required("admin")
def toggle_user(username):

    username = username.strip().lower()

    user_ref = users_ref.document(username)
    user_doc = user_ref.get()

    if not user_doc.exists:
        return "User not found", 404

    data = user_doc.to_dict()

    # Never allow the OWNER account to be deactivated.
    if data.get("role") == "OWNER":
        return "The OWNER account cannot be deactivated", 403

    current_status = data.get("active", True)

    user_ref.update({
        "active": not current_status
    })

    return redirect(url_for("user_management"))


@app.route("/tickets")
@role_required("support")
def tickets():

    search = request.args.get("search", "").strip().lower()

    game_filter = request.args.get("game", "").strip()
    status_filter = request.args.get("status", "").strip()
    result_filter = request.args.get("result", "").strip()
    date_filter = request.args.get("date", "").strip()

    print("========== FILTERS ==========")
    print("Search :", search)
    print("Game   :", game_filter)
    print("Status :", status_filter)
    print("Result :", result_filter)
    print("Date   :", date_filter)
    print("=============================")

    all_tickets = []

    docs = tickets_ref.stream()

    for doc in docs:

        if not doc.id.startswith("MPG"):
            continue

        data = doc.to_dict()

        if data.get("is_test"):
            continue

        data["reference"] = doc.id

        data.setdefault("game", "")
        data.setdefault("phone", "")
        data.setdefault("numbers", "")
        data.setdefault("boards", 0)
        data.setdefault("cost", 0)
        data.setdefault("played_at", "")
        data.setdefault("draw", "")
        data.setdefault("won", False)
        data.setdefault("winnings", 0)
        data.setdefault("result_checked", False)

        # -----------------------------
        # Game Filter
        # -----------------------------
        if game_filter:
            if data.get("game", "") != game_filter:
                continue

        # -----------------------------
        # Status Filter
        # -----------------------------
        if status_filter:

            if status_filter == "Pending" and data.get("result_checked"):
                continue

            if status_filter == "Processed" and not data.get("result_checked"):
                continue

        # -----------------------------
        # Result Filter
        # -----------------------------
        if result_filter:

            if result_filter == "Winner" and not data.get("won"):
                continue

            if result_filter == "Loser" and data.get("won"):
                continue

        # -----------------------------
        # Date Filter
        # -----------------------------
        if date_filter:

            played = data.get("played_at", "")

            if not played.startswith(date_filter):
                continue

        # -----------------------------
        # Search
        # -----------------------------
        if search:

            if (
                search not in doc.id.lower()
                and search not in data["phone"].lower()
                and search not in data["game"].lower()
            ):
                continue

        all_tickets.append(data)

    all_tickets.reverse()

    # -----------------------------
    # Pagination
    # -----------------------------
    per_page = int(request.args.get("per_page", 25))
    page = int(request.args.get("page", 1))

    total_tickets = len(all_tickets)

    start = (page - 1) * per_page
    end = start + per_page

    display_tickets = all_tickets[start:end]

    total_pages = (total_tickets + per_page - 1) // per_page

    # -----------------------------
    # Navigation Buttons
    # -----------------------------
    has_previous = page > 1
    has_next = page < total_pages

    previous_page = page - 1
    next_page = page + 1

    return render_template(
        "tickets.html",
        tickets=display_tickets,
        search=search,
        game_filter=game_filter,
        status_filter=status_filter,
        result_filter=result_filter,
        date_filter=date_filter,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        total_tickets=total_tickets,

        has_previous=has_previous,
        has_next=has_next,
        previous_page=previous_page,
        next_page=next_page,
        
    )

@app.route("/winners")
@role_required("finance")
def winners():

    winners = []

    docs = tickets_ref.stream()

    for doc in docs:

        if not doc.id.startswith("MPG"):
            continue

        data = doc.to_dict()

        if data.get("is_test"):
            continue

        if data.get("won"):

            data["reference"] = doc.id
            winners.append(data)

    winners.reverse()

    return render_template(
        "winners.html",
        winners=winners
    )

@app.route("/mark_paid/<reference>", methods=["POST"])
@role_required("finance")
def mark_paid(reference):

    tickets_ref.document(reference).update({

        "paid": True

    })

    return redirect("/winners")

@app.route("/draws", methods=["GET", "POST"])
@role_required("results")
def draws():

    draws_ref = db.collection("results")

    if request.method == "POST":

        game = request.form["game"]

        draw_id = request.form["draw_id"]

        numbers = request.form["numbers"]

        jackpot = int(request.form["jackpot"])

        document = f"{draw_id}_{game.upper().replace(' ','')}"

        draws_ref.document(document).set({

            "draw_id": draw_id,
            "game": game,
            "numbers": numbers,
            "jackpot": jackpot,

            "processed": False,
            "processed_at": ""

        })

    all_draws = []

    for doc in draws_ref.stream():
        data = doc.to_dict()
        all_draws.append(data)

    all_draws.sort(
        key=lambda x: x.get("draw_id", ""),
        reverse=True
    )

    return render_template(
        "draws.html",
        draws=all_draws
    )

@app.route("/reports")
@role_required("finance")
def reports():

    ticket_docs = list(tickets_ref.stream())

    reports = {

        "tickets": 0,

        "revenue": 0,

        "winners": 0,

        "losers": 0,

        "pending": 0,

        "prize_total": 0,

        "paid": 0,

        "awaiting_payment": 0,

        "profit": 0,

        "win_rate": 0

    }
    game_stats = {}

    for ticket in ticket_docs:

        if not ticket.id.startswith("MPG"):
            continue

        data = ticket.to_dict()

        if data.get("is_test"):
            continue

        reports["tickets"] += 1

        game = data.get("game", "Unknown")

        if game not in game_stats:

            game_stats[game] = {

                "tickets": 0,
                "winners": 0,
                "revenue": 0,
                "prize": 0

            }

        game_stats[game]["tickets"] += 1
        ticket_cost = data.get("cost")
        if ticket_cost is None:
            ticket_cost = data.get("amount")
        if ticket_cost is None:
            ticket_cost = get_game_price(game)
        ticket_cost = ticket_cost or 0

        game_stats[game]["revenue"] += ticket_cost
        reports["revenue"] += ticket_cost

        if data.get("result_checked"):

            if data.get("won"):

                reports["winners"] += 1
                reports["prize_total"] += data.get("winnings", 0)

                payment_status = data.get("payment_status")

                if not payment_status:
                    payment_status = "AWAITING SIZEKHAYA SETTLEMENT"

                if payment_status == "SETTLED":
                    reports["paid"] += 1
                elif payment_status in ["AWAITING SIZEKHAYA SETTLEMENT", "SUBMITTED TO SIZEKHAYA"]:
                    reports["awaiting_payment"] += 1

            else:

                reports["losers"] += 1

        else:

            reports["pending"] += 1

    reports["profit"] = reports["revenue"] - reports["prize_total"]

    if reports["tickets"] > 0:

        reports["win_rate"] = round(
            (reports["winners"] / reports["tickets"]) * 100,
            1
        )

    from collections import defaultdict

    daily_sales = defaultdict(int)
    daily_revenue = defaultdict(int)

    for ticket in ticket_docs:

        if not ticket.id.startswith("MPG"):
            continue

        data = ticket.to_dict()

        if data.get("is_test"):
            continue

        played_at = data.get("played_at", "")

        if played_at:

            try:
                parsed_played_at = datetime.strptime(played_at, "%d/%m/%Y %H:%M")
            except ValueError:
                try:
                    parsed_played_at = datetime.strptime(played_at, "%d %B %Y %H:%M")
                except ValueError:
                    continue

            day = parsed_played_at.strftime("%d/%m/%Y")

            daily_sales[day] += 1
            ticket_cost = data.get("cost")
            if ticket_cost is None:
                ticket_cost = data.get("amount")
            if ticket_cost is None:
                ticket_cost = get_game_price(data.get("game"))
            daily_revenue[day] += ticket_cost or 0

    sorted_days = sorted(daily_sales.keys(), key=lambda d: datetime.strptime(d, "%d/%m/%Y"))

    reports["sales_labels"] = sorted_days
    reports["sales_values"] = [daily_sales[d] for d in sorted_days]

    reports["revenue_labels"] = sorted_days
    reports["revenue_values"] = [daily_revenue[d] for d in sorted_days]


    print("Revenue Labels:", reports["revenue_labels"])
    print("Revenue Values:", reports["revenue_values"])    

    return render_template(

        "reports.html",

        reports=reports,

        game_stats=game_stats

    )

@app.route("/process/<draw_id>/<game>", methods=["POST"])
@role_required("results")
def process(draw_id, game):

    print("=" * 60)
    print("PROCESS ROUTE CALLED")
    print("Draw ID:", draw_id)
    print("Game:", game)
    print("=" * 60)

    process_draw_results(draw_id, game)

    document = f"{draw_id}_{game.upper().replace(' ','')}"

    db.collection("results").document(document).update({

        "processed": True,
        "processed_at": datetime.now().strftime("%Y-%m-%d %H:%M")

    })

    updated = db.collection("results").document(document).get()

    print("=" * 60)
    print("UPDATED DOCUMENT")
    print(updated.to_dict())
    print("=" * 60)

    return redirect("/draws")



@app.route("/ticket/<reference>")
@role_required("support")
def ticket_details(reference):

    print()
    print("=" * 70)
    print("ENTERED TICKET DETAILS ROUTE")
    print("Reference:", reference)
    print("=" * 70)

    ticket = None

    # Try document ID first
    doc = tickets_ref.document(reference).get()

    if doc.exists:

        ticket_data = doc.to_dict()

        # Operational ticket details only show real MPG tickets
        if not doc.id.startswith("MPG") or ticket_data.get("is_test"):
            return "Ticket not found", 404

        ticket = ticket_data

        print(ticket.keys())

        ticket["reference"] = doc.id

        ticket["draw_numbers"] = ticket.get("draw_numbers", "")
        ticket["matched_numbers"] = ticket.get("matched_numbers", [])
        ticket["matches"] = ticket.get("matches", 0)
        ticket["processed_at"] = ticket.get("processed_at", "Not Recorded")
        ticket["sms_sent"] = ticket.get("sms_sent", False)

        print()
        print("=" * 60)
        print("TICKET VALUES")
        print("Draw Numbers   :", repr(ticket["draw_numbers"]))
        print("Matched Numbers:", repr(ticket["matched_numbers"]))
        print("=" * 60)
        print()

        print("=" * 60)
        print("SMS SENT FROM TICKET ROUTE")
        print(ticket["sms_sent"])
        print("=" * 60)

        if not ticket.get("won"):
            ticket["payment_status"] = "NOT APPLICABLE"

    else:
        docs = tickets_ref.where(
            filter=FieldFilter("reference", "==", reference)
        ).stream()

        for d in docs:
            ticket = d.to_dict()
            ticket["reference"] = d.id
            break

    if not ticket:
        return "Ticket not found", 404


    # ==========================================
    # CUSTOMER STATISTICS
    # ==========================================

    customer_phone = ticket.get("phone")

    customer_docs = tickets_ref.where(
        filter=FieldFilter("phone", "==", customer_phone)
    ).stream()

    total_tickets = 0
    total_spent = 0
    total_winnings = 0
    total_wins = 0
    last_played = ""

    for doc in customer_docs:

        t = doc.to_dict()

        # Match operational customer statistics
        if not doc.id.startswith("MPG") or t.get("is_test"):
            continue

        winnings = t.get("winnings", 0)
        won = t.get("won", False)

        total_tickets += 1
        total_spent += t.get("cost", 0)
        total_winnings += winnings

        if won:
            total_wins += 1

        played = t.get("played_at", "")

        if played > last_played:
            last_played = played

    if total_tickets:
        win_rate = round((total_wins / total_tickets) * 100, 2)
    else:
        win_rate = 0

    customer = {
        "phone": customer_phone,
        "total_tickets": total_tickets,
        "total_spent": total_spent,
        "total_winnings": total_winnings,
        "total_wins": total_wins,
        "win_rate": win_rate,
        "last_played": last_played
    }


    return render_template(
        "ticket_details.html",
        ticket=ticket,
        customer=customer
    )

@app.route("/payments")
@role_required("finance")
def payments():

    tickets = []

    total_winners = 0
    awaiting_payment = 0
    paid_count = 0
    total_paid = 0
    outstanding = 0

    docs = (
        tickets_ref
        .where(filter=FieldFilter("won", "==", True))
        .stream()
    )
    for doc in docs:

        if not doc.id.startswith("MPG"):
            continue

        data = doc.to_dict()

        if data.get("is_test"):
            continue

        data["id"] = doc.id

        # Make sure the reference is available
        data["reference"] = data.get("reference") or doc.id

        # Default winning tickets to awaiting Sizekhaya settlement
        if not data.get("payment_status"):
            data["payment_status"] = "AWAITING SIZEKHAYA SETTLEMENT"

        tickets.append(data)

        total_winners += 1

        winnings = data.get("winnings", 0) or 0

        if data.get("payment_status") == "SETTLED":

            paid_count += 1

            total_paid += winnings

        else:

            awaiting_payment += 1

            outstanding += winnings

    return render_template(

        "prize_settlement.html",

        tickets=tickets,

        total_winners=total_winners,

        awaiting_payment=awaiting_payment,

        paid_count=paid_count,

        total_paid=total_paid,

        outstanding=outstanding

    )

# =========================================
# MIGRATE OLD PRIZE SETTLEMENT STATUSES
# =========================================

@app.route("/admin/migrate-settlement-status")
@role_required("maintenance")
def migrate_settlement_status():

    print()
    print("=" * 60)
    print("MIGRATING OLD SETTLEMENT STATUSES")
    print("=" * 60)

    updated_count = 0

    docs = (
        tickets_ref
        .where(filter=FieldFilter("won", "==", True))
        .stream()
    )

    for doc in docs:

        data = doc.to_dict()

        current_status = data.get(
            "payment_status"
        )

        # Only migrate old winning tickets
        # that still use the legacy status.
        if current_status == "AWAITING PAYMENT":

            doc.reference.update({

                "payment_status":
                    "AWAITING SIZEKHAYA SETTLEMENT"

            })

            updated_count += 1

            print(
                "UPDATED:",
                data.get("reference", doc.id)
            )

    print()
    print("TOTAL UPDATED:", updated_count)
    print("=" * 60)

    return (
        f"Migration complete. "
        f"{updated_count} winning ticket(s) updated."
    )


# =========================================
# REVIEW PRIZE SETTLEMENT
# =========================================

@app.route("/settlement/<reference>")
@role_required("finance")
def review_settlement(reference):

    print()
    print("=" * 60)
    print("REVIEWING PRIZE SETTLEMENT")
    print("Reference:", reference)
    print("=" * 60)

    ticket = None

    # =========================================================
    # FIND TICKET
    # =========================================================

    doc = tickets_ref.document(reference).get()

    if doc.exists:

        ticket = doc.to_dict()
        ticket["reference"] = doc.id

    else:

        docs = tickets_ref.where(
            filter=FieldFilter("reference", "==", reference)
        ).stream()

        for d in docs:

            ticket = d.to_dict()
            ticket["reference"] = d.id

            break

    # =========================================================
    # TICKET DOES NOT EXIST
    # =========================================================

    if not ticket:

        return "Winning ticket not found", 404

    # =========================================================
    # ONLY REAL MPG TICKETS CAN ENTER SETTLEMENT
    # =========================================================

    if not ticket.get("reference", "").startswith("MPG"):

        return "Winning ticket not found", 404

    if ticket.get("is_test"):

        return "Winning ticket not found", 404

    # =========================================================
    # MUST BE A WINNING TICKET
    # =========================================================

    if not ticket.get("won"):

        return "This ticket is not a winning ticket", 400

    # =========================================================
    # NORMALISE NUMBERS
    # =========================================================

    def normalise_numbers(value):

        if value is None:

            return []

        if isinstance(value, (list, tuple, set)):

            values = list(value)

        elif isinstance(value, str):

            value = value.strip()

            if not value:

                return []

            value = value.replace("[", "")
            value = value.replace("]", "")
            value = value.replace("(", "")
            value = value.replace(")", "")

            values = value.split(",")

        else:

            values = [value]

        cleaned = []

        for number in values:

            number = str(number).strip()

            if not number:

                continue

            try:

                cleaned.append(int(number))

            except ValueError:

                cleaned.append(number)

        return cleaned

    # =========================================================
    # TICKET NUMBERS
    # =========================================================

    ticket_numbers = normalise_numbers(
        ticket.get("numbers")
    )

    # =========================================================
    # ORIGINAL MPG DRAW NUMBERS
    # =========================================================

    draw_numbers = normalise_numbers(
        ticket.get("draw_numbers")
    )

    # =========================================================
    # STORED MATCHED NUMBERS
    # =========================================================

    matched_numbers = normalise_numbers(
        ticket.get("matched_numbers")
    )

    # =========================================================
    # ORIGINAL RECORDED MATCHES
    # =========================================================

    original_recorded_matches = ticket.get(
        "original_recorded_matches",
        ticket.get("matches", 0)
    )

    try:

        original_recorded_matches = int(
            original_recorded_matches or 0
        )

    except (ValueError, TypeError):

        original_recorded_matches = 0

    # =========================================================
    # ORIGINAL RECORDED WINNINGS
    # =========================================================

    original_recorded_winnings = ticket.get(
        "original_recorded_winnings",
        ticket.get("winnings", 0)
    )

    try:

        original_recorded_winnings = float(
            original_recorded_winnings or 0
        )

    except (ValueError, TypeError):

        original_recorded_winnings = 0

    # =========================================================
    # DISPLAY MATCH COUNT
    # =========================================================

    if matched_numbers:

        matches = len(matched_numbers)

    else:

        matches = original_recorded_matches

    # =========================================================
    # RESULT CHECKED
    # =========================================================

    result_checked = bool(
        ticket.get(
            "result_checked",
            False
        )
    )

    # =========================================================
    # ORIGINAL PROCESSED TIME
    # =========================================================

    processed_at = ticket.get(
        "processed_at",
        ""
    )

    # =========================================================
    # OFFICIAL HISTORICAL DRAW RESULT
    # =========================================================

    official_draw_numbers = normalise_numbers(
        ticket.get(
            "official_draw_numbers",
            []
        )
    )

    # =========================================================
    # OFFICIAL BONUS
    # =========================================================

    official_bonus_number = ticket.get(
        "official_bonus_number"
    )

    # Normalise bonus to integer where possible

    if official_bonus_number not in (
        None,
        "",
        "N/A"
    ):

        try:

            official_bonus_number = int(
                official_bonus_number
            )

        except (ValueError, TypeError):

            pass

    # =========================================================
    # OFFICIAL MATCHED NUMBERS
    # =========================================================

    official_matched_numbers = normalise_numbers(
        ticket.get(
            "official_matched_numbers",
            []
        )
    )

    # =========================================================
    # OFFICIAL MATCH COUNT
    # =========================================================

    official_matches = ticket.get(
        "official_matches",
        0
    )

    try:

        official_matches = int(
            official_matches or 0
        )

    except (ValueError, TypeError):

        official_matches = 0

    # =========================================================
    # OFFICIAL WINNINGS
    #
    # This is optional for now.
    # If it does not exist, we use None.
    # =========================================================

    official_winnings = ticket.get(
        "official_winnings"
    )

    if official_winnings is not None:

        try:

            official_winnings = float(
                official_winnings
            )

        except (ValueError, TypeError):

            official_winnings = None

    # =========================================================
    # DETERMINE VERIFICATION STATUS
    #
    # THIS IS THE ONLY PLACE WHERE THE STATUS IS DECIDED.
    # =========================================================

    stored_verification_status = ticket.get(
        "verification_status"
    )

    # ---------------------------------------------------------
    # CASE 1
    # Official historical result exists
    # ---------------------------------------------------------

    if official_draw_numbers:

        # Compare the official historical result
        # against the original MPG recorded result.

        if original_recorded_matches != official_matches:

            verification_status = (
                "HISTORICAL_RESULT_CONFLICT"
            )

        elif (
            official_winnings is not None
            and
            original_recorded_winnings != official_winnings
        ):

            verification_status = (
                "HISTORICAL_RESULT_CONFLICT"
            )

        else:

            verification_status = "VERIFIED"

    # ---------------------------------------------------------
    # CASE 2
    # No official result, but ticket was processed
    # ---------------------------------------------------------

    elif result_checked:

        verification_status = (
            "HISTORICAL_DATA_INCOMPLETE"
        )

    # ---------------------------------------------------------
    # CASE 3
    # Ticket has not been checked
    # ---------------------------------------------------------

    else:

        verification_status = "PENDING"

    # =========================================================
    # IMPORTANT:
    # If a historical conflict was explicitly recorded,
    # never downgrade it because processed_at is missing.
    # =========================================================

    if stored_verification_status == (
        "HISTORICAL_RESULT_CONFLICT"
    ):

        verification_status = (
            "HISTORICAL_RESULT_CONFLICT"
        )

    # =========================================================
    # SETTLEMENT INFORMATION
    # =========================================================

    settlement = {

        "reference": ticket.get(
            "reference"
        ),

        "phone": ticket.get(
            "phone"
        ),

        "game": ticket.get(
            "game"
        ),

        "draw_id": ticket.get(
            "draw_id"
        ),

        "draw": ticket.get(
            "draw"
        ),

        "numbers": ticket.get(
            "numbers"
        ),

        # Original MPG draw result
        "draw_numbers": draw_numbers,

        # Original MPG matched numbers
        "matched_numbers": matched_numbers,

        # Original MPG match count
        "matches": matches,

        # Original MPG prize
        "prize": ticket.get(
            "winnings",
            0
        ),

        "payment_status": ticket.get(
            "payment_status",
            "AWAITING SIZEKHAYA SETTLEMENT"
        ),

        "result_checked": result_checked,

        "sms_sent": ticket.get(
            "sms_sent",
            False
        ),

        "processed_at": processed_at,

        # =====================================================
        # FINAL AUTHORITATIVE VERIFICATION STATUS
        # =====================================================

        "verification_status": verification_status,

        # =====================================================
        # OFFICIAL HISTORICAL RESULT
        # =====================================================

        "official_draw_numbers":
            official_draw_numbers,

        "official_bonus_number":
            official_bonus_number,

        "official_matched_numbers":
            official_matched_numbers,

        "official_matches":
            official_matches,

        # =====================================================
        # ORIGINAL MPG RECORD
        # =====================================================

        "original_recorded_matches":
            original_recorded_matches,

        "original_recorded_winnings":
            original_recorded_winnings,

        # =====================================================
        # SETTLEMENT TIMELINE
        # =====================================================

        "settlement_submitted_at":
            ticket.get(
                "settlement_submitted_at",
                ""
            ),

        "settled_at":
            ticket.get(
                "settled_at",
                ""
            )
    }

    # =========================================================
    # DEBUG INFORMATION
    # =========================================================

    print()
    print("SETTLEMENT INFORMATION")

    print(
        "Reference       :",
        settlement["reference"]
    )

    print(
        "Phone           :",
        settlement["phone"]
    )

    print(
        "Game            :",
        settlement["game"]
    )

    print(
        "Draw ID         :",
        settlement["draw_id"]
    )

    print(
        "Prize           :",
        settlement["prize"]
    )

    print(
        "Ticket Numbers  :",
        ticket_numbers
    )

    print(
        "Draw Numbers    :",
        draw_numbers
    )

    print(
        "Matched Numbers :",
        matched_numbers
    )

    print(
        "Matches         :",
        matches
    )

    print(
        "Result Checked  :",
        result_checked
    )

    print(
        "Original Processed At :",
        processed_at
    )

    print(
        "Original Matches      :",
        original_recorded_matches
    )

    print(
        "Original Winnings     :",
        original_recorded_winnings
    )

    print(
        "Official Draw Numbers :",
        official_draw_numbers
    )

    print(
        "Official Bonus        :",
        official_bonus_number
    )

    print(
        "Official Matched      :",
        official_matched_numbers
    )

    print(
        "Official Matches      :",
        official_matches
    )

    print(
        "Verification Status   :",
        verification_status
    )

    print(
        "Payment Status        :",
        settlement["payment_status"]
    )

    print("=" * 60)

    return render_template(
        "review_settlement.html",
        ticket=ticket,
        settlement=settlement
    )

@app.route("/repair-historical-ticket/<reference>")
@role_required("maintenance")
def repair_historical_ticket(reference):

    print()
    print("=" * 70)
    print("HISTORICAL TICKET REPAIR")
    print("Reference:", reference)
    print("=" * 70)

    # =========================================================
    # FIND TICKET
    # =========================================================

    doc = tickets_ref.document(reference).get()

    if not doc.exists:

        return "Ticket not found", 404

    ticket = doc.to_dict()

    game = ticket.get("game")
    draw_id = ticket.get("draw_id")

    print("Game:", game)
    print("Draw ID:", draw_id)

    # =========================================================
    # THIS REPAIR IS ONLY FOR THE KNOWN HISTORICAL TICKET
    # =========================================================

    if reference != "MPG4AE7C1C":

        return "This repair route is only for MPG4AE7C1C", 403

    # =========================================================
    # OFFICIAL 17 JUNE 2026 LOTTO RESULT
    # =========================================================

    official_numbers = [
        8,
        34,
        36,
        42,
        51,
        52
    ]

    official_bonus = 18

    # =========================================================
    # GET TICKET NUMBERS
    # =========================================================

    ticket_numbers = ticket.get("numbers", [])

    if isinstance(ticket_numbers, str):

        ticket_numbers = [
            int(x.strip())
            for x in ticket_numbers.split(",")
            if x.strip()
        ]

    else:

        ticket_numbers = [
            int(x)
            for x in ticket_numbers
        ]

    # =========================================================
    # CALCULATE OFFICIAL MATCHES
    # =========================================================

    official_matched_numbers = sorted(
        set(ticket_numbers) & set(official_numbers)
    )

    official_matches = len(
        official_matched_numbers
    )

    # =========================================================
    # UPDATE FIRESTORE
    # =========================================================

    tickets_ref.document(reference).update({

        # Official result
        "official_draw_numbers": official_numbers,

        "official_bonus_number": official_bonus,

        "official_matched_numbers":
            official_matched_numbers,

        "official_matches":
            official_matches,

        # Historical verification
        "verification_status":
            "HISTORICAL_RESULT_CONFLICT",

        "historical_data_repaired": True,

        "historical_repair_at":
            datetime.now().strftime(
                "%d/%m/%Y %H:%M:%S"
            ),

        "historical_repair_note":
            "Historical ticket was previously recorded as a winner. "
            "Official 17 June 2026 Lotto result was verified as "
            "8,34,36,42,51,52 with bonus 18. "
            "Ticket numbers 1,2,3,4,5,6 produced 0 official matches. "
            "Original historical settlement fields were preserved."
    })

    print()
    print("=" * 70)
    print("HISTORICAL REPAIR COMPLETE")
    print("=" * 70)

    print("Reference:",
          reference)

    print("Ticket Numbers:",
          ticket_numbers)

    print("Official Numbers:",
          official_numbers)

    print("Official Bonus:",
          official_bonus)

    print("Official Matches:",
          official_matches)

    print("Official Matched Numbers:",
          official_matched_numbers)

    print("Original Recorded Matches:",
          ticket.get("matches"))

    print("Original Recorded Winnings:",
          ticket.get("winnings"))

    print("Verification Status:",
          "HISTORICAL_RESULT_CONFLICT")

    print("=" * 70)

    return redirect(
        url_for(
            "review_settlement",
            reference=reference
        )
    )

@app.route("/admin/repair-settlement/<reference>", methods=["POST"])
@role_required("maintenance")
def repair_settlement(reference):

    print()
    print("=" * 60)
    print("REPAIRING SETTLEMENT DATA")
    print("Reference:", reference)
    print("=" * 60)

    # =========================================================
    # FIND TICKET
    # =========================================================

    doc = tickets_ref.document(reference).get()

    if not doc.exists:
        return "Ticket not found", 404

    ticket = doc.to_dict()

    # =========================================================
    # ONLY REPAIR WINNING TICKETS
    # =========================================================

    if not ticket.get("won"):
        return "This ticket is not a winning ticket", 400

    # =========================================================
    # NORMALISE NUMBERS
    # =========================================================

    def normalise_numbers(value):

        if value is None:
            return []

        if isinstance(value, (list, tuple, set)):
            values = list(value)

        elif isinstance(value, str):

            value = value.strip()

            if not value:
                return []

            value = value.replace("[", "")
            value = value.replace("]", "")
            value = value.replace("(", "")
            value = value.replace(")", "")

            values = value.split(",")

        else:
            values = [value]

        cleaned = []

        for number in values:

            number = str(number).strip()

            if not number:
                continue

            try:
                cleaned.append(int(number))
            except ValueError:
                cleaned.append(number)

        return cleaned

    # =========================================================
    # GET NUMBERS
    # =========================================================

    ticket_numbers = normalise_numbers(
        ticket.get("numbers")
    )

    draw_numbers = normalise_numbers(
        ticket.get("draw_numbers")
    )

    # =========================================================
    # CALCULATE MATCHED NUMBERS
    # =========================================================

    matched_numbers = sorted(
        set(ticket_numbers) & set(draw_numbers)
    )

    matches = len(matched_numbers)

    print("Ticket Numbers  :", ticket_numbers)
    print("Draw Numbers    :", draw_numbers)
    print("Matched Numbers :", matched_numbers)
    print("Matches         :", matches)

    # =========================================================
    # RESULT VERIFICATION TIMESTAMP
    # =========================================================

    processed_at = ticket.get("processed_at")

    if not processed_at:

        processed_at = datetime.now().strftime(
            "%d/%m/%Y %H:%M:%S"
        )

        print(
            "Missing processed_at."
        )

        print(
            "Repair timestamp:",
            processed_at
        )

    # =========================================================
    # UPDATE FIRESTORE
    # =========================================================

    tickets_ref.document(reference).update({

        "matches": matches,

        "matched_numbers": matched_numbers,

        "result_checked": True,

        "processed_at": processed_at,

        "draw_numbers": draw_numbers

    })

    print()
    print("SETTLEMENT DATA REPAIRED")
    print("Reference       :", reference)
    print("Matched Numbers :", matched_numbers)
    print("Matches         :", matches)
    print("Processed At    :", processed_at)
    print("=" * 60)

    return redirect(
        url_for(
            "review_settlement",
            reference=reference
        )
    )

@app.route("/settlement/<reference>/confirm", methods=["POST"])
@role_required("finance")
def confirm_settlement(reference):

    print("\n" + "=" * 60)
    print("CONFIRMING PRIZE SETTLEMENT")
    print(f"Reference: {reference}")
    print("=" * 60)

    # Find the ticket
    doc = tickets_ref.document(reference).get()

    if not doc.exists:
        return "Winning ticket not found", 404

    data = doc.to_dict()

    # =========================================================
    # ONLY REAL MPG TICKETS CAN ENTER SETTLEMENT
    # =========================================================

    if not reference.startswith("MPG"):

        return "Winning ticket not found", 404

    if data.get("is_test"):

        return "Winning ticket not found", 404

    current_status = data.get("payment_status")
    winnings = data.get("winnings", 0) or 0

    print(f"Current Status : {current_status}")
    print(f"Prize          : R{winnings}")

    # Only submitted tickets can be confirmed as settled
    if current_status != "SUBMITTED TO SIZEKHAYA":
        return (
            f"Ticket cannot be settled. "
            f"Current status is: {current_status}"
        ), 400

    # Settlement confirmation timestamp
    settlement_confirmed_at = datetime.now().strftime(
        "%d/%m/%Y %H:%M:%S"
    )

    # Update Firestore
    tickets_ref.document(reference).update({

        "payment_status": "SETTLED",

        "settled_at": settlement_confirmed_at,

        "settlement_confirmed": True,

        "settlement_confirmed_at": settlement_confirmed_at

    })

    print("\nSETTLEMENT CONFIRMED")
    print(f"Reference       : {reference}")
    print(f"Prize           : R{winnings}")
    print(f"Confirmed At    : {settlement_confirmed_at}")
    print("=" * 60)

    return redirect(
        url_for(
            "review_settlement",
            reference=reference
        )
    )

# =========================================
# SUBMIT PRIZE SETTLEMENT TO SIZEKHAYA
# =========================================

@app.route("/settlement/submit/<reference>", methods=["POST"])
@role_required("finance")
def submit_settlement(reference):

    print()
    print("=" * 60)
    print("SUBMITTING PRIZE SETTLEMENT")
    print("Reference:", reference)
    print("=" * 60)

    docs = tickets_ref.where(
        filter=FieldFilter("reference", "==", reference)
    ).stream()

    ticket_doc = None
    ticket_data = None

    for doc in docs:

        ticket_doc = doc
        ticket_data = doc.to_dict()

        break

    if not ticket_doc:

        return "Winning ticket not found", 404

    # =========================================================
    # ONLY REAL MPG TICKETS CAN ENTER SETTLEMENT
    # =========================================================

    if not ticket_data.get("reference", "").startswith("MPG"):

        return "Winning ticket not found", 404

    if ticket_data.get("is_test"):

        return "Winning ticket not found", 404

    # =========================================================
    # MUST BE A WINNING TICKET
    # =========================================================

    if not ticket_data.get("won"):

        return "This ticket is not a winning ticket", 400

    current_status = ticket_data.get(
        "payment_status",
        "AWAITING SIZEKHAYA SETTLEMENT"
    )

    # =========================================================
    # ONLY UNSETTLED WINNERS CAN BE SUBMITTED
    # =========================================================

    if current_status != "AWAITING SIZEKHAYA SETTLEMENT":

        return (
            f"Ticket cannot be submitted. "
            f"Current status is: {current_status}"
        ), 400

    # Create settlement tracking record
    settlement_record = {

        "reference": ticket_data.get("reference"),

        "phone": ticket_data.get("phone"),

        "game": ticket_data.get("game"),

        "prize": ticket_data.get(
            "winnings",
            0
        ),

        "draw_id": ticket_data.get("draw_id"),

        "submitted_at": current_date(),

        "status": "SUBMITTED TO SIZEKHAYA",

        "operator": "SIZEKHAYA",

        "channel": "MPG"

    }

    db.collection("settlements").document(
        reference
    ).set(
        settlement_record
    )

    # Update MPG ticket tracking status
    ticket_doc.reference.update({

        "payment_status": "SUBMITTED TO SIZEKHAYA",

        "settlement_submitted_at": current_date(),

        "settlement_method": "SIZEKHAYA",

        "settlement_operator": "Lottery Operator"

    })

    print()
    print("SETTLEMENT SUBMITTED")
    print("Reference:", reference)
    print("Prize: R", ticket_data.get("winnings", 0))
    print("=" * 60)

    return redirect(
        f"/settlement/{reference}"
    ) 

@app.route("/customers")
@role_required("support")
def customers():

    search = request.args.get("search", "").strip()

    customers = {}

    tickets = tickets_ref.stream()

    for ticket in tickets:

        if not ticket.id.startswith("MPG"):
            continue

        data = ticket.to_dict()

        if data.get("is_test"):
            continue

        phone = data.get("phone", "Unknown")

        if phone not in customers:

            customers[phone] = {

                "phone": phone,
                "tickets": 0,
                "spent": 0,
                "wins": 0,
                "winnings": 0,
                "last_played": ""

            }

        customers[phone]["tickets"] += 1
        customers[phone]["spent"] += data.get("cost", 0)

        if data.get("won"):

            customers[phone]["wins"] += 1
            customers[phone]["winnings"] += data.get("winnings", 0)

        played = data.get("played_at", "")

        if played > customers[phone]["last_played"]:

            customers[phone]["last_played"] = played

    if search:

        search_value = (
            search
            .replace("+", "")
            .replace(" ", "")
            .replace("-", "")
        )

        # Convert local format (0...) to international format (27...)
        if search_value.startswith("0"):
            search_value = "27" + search_value[1:]

        filtered = {}

        for phone, customer in customers.items():

            phone_value = (
                phone
                .replace("+", "")
                .replace(" ", "")
                .replace("-", "")
            )

            if (
                search in phone
                or search_value in phone_value
            ):
                filtered[phone] = customer

        customers = filtered

    print(customers)

    return render_template(

        "customers.html",

        customers=list(customers.values()),

        search=search

    )

@app.route("/sms")
@role_required("sms")
def sms_management():

    sms_logs = []

    try:

        docs = db.collection("sms_logs") \
                 .order_by("sent_at", direction=firestore.Query.DESCENDING) \
                 .stream()

        for doc in docs:

            sms = doc.to_dict()

            sms_logs.append(sms)

    except Exception as e:

        print("SMS Error:", e)

    return render_template(

        "sms_management.html",

        sms_logs=sms_logs

    )

@app.route("/customer/<phone>")
@role_required("support")
def customer_profile(phone):

    customer = {
        "phone": phone,
        "tickets": 0,
        "spent": 0,
        "wins": 0,
        "winnings": 0,
        "last_played": ""
    }

    customer_tickets = []

    tickets = tickets_ref.stream()

    for ticket in tickets:

        data = ticket.to_dict()

        if data.get("phone") != phone:
            continue

        if not ticket.id.startswith("MPG"):
            continue

        if data.get("is_test"):
            continue

        # Update customer statistics
        customer["tickets"] += 1
        customer["spent"] += data.get("cost", 0)

        if data.get("won"):
            customer["wins"] += 1
            customer["winnings"] += data.get("winnings", 0)

        played = data.get("played_at", "")

        if played > customer["last_played"]:
            customer["last_played"] = played

        # Only add complete tickets to the history table
        reference = data.get("reference", "")

        if reference:
            customer_tickets.append({
                "reference": reference,
                "game": data.get("game", ""),
                "draw": data.get("draw", ""),
                "played_at": data.get("played_at", ""),
                "status": data.get("status", ""),
                "payment_status": data.get("payment_status", ""),
                "winnings": data.get("winnings", 0)
            })

        if customer["tickets"] > 0:
            customer["win_rate"] = round(
                (customer["wins"] / customer["tickets"]) * 100,
                2
            )
        else:
            customer["win_rate"] = 0  

        customer_tickets.sort(
            key=lambda t: t.get("played_at", ""),
            reverse=True
        )  

    return render_template(
        "customer_profile.html",
        customer=customer,
        tickets=customer_tickets
    )           

@app.route("/draw/<draw_id>/<game>")
@role_required("results")
def draw_summary(draw_id, game):

    # -----------------------------
    # Get Draw Information
    # -----------------------------
    document = f"{draw_id}_{game.upper().replace(' ','')}"

    draw_doc = db.collection("results").document(document).get()

    if not draw_doc.exists:
        return "Draw not found."

    draw = draw_doc.to_dict()

    # -----------------------------
    # Get Statistics From Results
    # -----------------------------
    winners = 0
    losers = 0
    pending = 0

    prize_total = 0

    ticket_sales = 0

    revenue = 0

    # -----------------------------
    # Load Tickets
    # -----------------------------
    ticket_docs = (
        db.collection("tickets")
        .where(filter=FieldFilter("draw_id", "==", draw_id))
        .where(filter=FieldFilter("game", "==", game))
        .stream()
    )

    tickets = []

    for doc in ticket_docs:

        print("LOOP STARTED")

        ticket = doc.to_dict()

        ticket["id"] = doc.id

        print(ticket.get("reference"))

        tickets.append(ticket)

        ticket_sales += 1

        ticket_cost = ticket.get("cost")
        if ticket_cost is None:
            ticket_cost = ticket.get("amount")
        if ticket_cost is None:
            ticket_cost = get_game_price(ticket.get("game"))
        revenue += ticket_cost or 0

        if not ticket.get("result_checked", False):

            pending += 1

        elif ticket.get("won"):

            winners += 1
            prize_total += ticket.get("winnings", 0)

        else:

            losers += 1      

    # -----------------------------
    # Render Page
    # -----------------------------
    print("=" * 60)
    print("DRAW SUMMARY DEBUG")
    print("=" * 60)

    print("Tickets list length:", len(tickets))

    for i, ticket in enumerate(tickets):
        print(f"Ticket {i + 1}:")
        print(ticket)

    print("=" * 60)

    profit = revenue - prize_total

    return render_template(

        "draw_summary.html",

        draw=draw,

        tickets=tickets,

        winners=winners,

        losers=losers,

        pending=pending,

        prize_total=prize_total,

        ticket_sales=ticket_sales,

        revenue=revenue,

        profit=profit

    )

# =========================================
# HELPERS
# =========================================
def receipt():

    return "MPG" + str(uuid.uuid4())[:7].upper()

def current_date():

    return datetime.now().strftime(
        "%d/%m/%Y %H:%M"
    )

# =========================================
# DETERMINE NEXT DRAW DATE
# =========================================
def get_next_draw_date(game):

    now = datetime.now()

    # Monday=0, Tuesday=1, Wednesday=2,
    # Thursday=3, Friday=4, Saturday=5, Sunday=6
    weekday = now.weekday()

    # -----------------------------------------
    # DAILY LOTTO
    # -----------------------------------------
    if game == "Daily Lotto":

        draw_date = now

        # If today's draw time has already passed,
        # ticket belongs to tomorrow's draw.
        if now.hour >= 21:
            draw_date = now + timedelta(days=1)

        return draw_date.strftime("%Y-%m-%d")

    # -----------------------------------------
    # LOTTO / LOTTO PLUS
    # Wednesday and Saturday
    # -----------------------------------------
    if game in ["Lotto", "Lotto Plus"]:

        draw_days = [2, 5]

        for days_ahead in range(7):

            candidate = now + timedelta(days=days_ahead)

            if candidate.weekday() in draw_days:

                # Wednesday/Saturday draw cutoff
                if days_ahead == 0 and (now.hour, now.minute) >= (20, 56):
                    continue

                return candidate.strftime("%Y-%m-%d")

    # -----------------------------------------
    # POWERBALL / POWERBALL PLUS
    # Tuesday and Friday
    # -----------------------------------------
    if game in ["PowerBall", "PowerBall Plus"]:

        draw_days = [1, 4]

        for days_ahead in range(7):

            candidate = now + timedelta(days=days_ahead)

            if candidate.weekday() in draw_days:

                # PowerBall draw cutoff
                if days_ahead == 0 and now.hour >= 21:
                    continue

                return candidate.strftime("%Y-%m-%d")

    # -----------------------------------------
    # SAFETY FALLBACK
    # -----------------------------------------
    return now.strftime("%Y-%m-%d")


def draw_details(game):

    draws = {

        "Lotto":
            "Wed/Sat 20:56",

        "Lotto Plus":
            "Wed/Sat 21:00",

        "PowerBall":
            "Tue/Fri 21:00",

        "PowerBall Plus":
            "Tue/Fri 21:05",

        "Daily Lotto":
            "Daily 21:00"
    }

    return draws.get(
        game,
        "Next Draw Soon"
    )

def boards_menu():

    return (
        "CON Boards\n"
        "1.1 Board\n"
        "2.2 Boards\n"
        "3.3 Boards\n"
        "4.4 Boards\n"
        "5.5 Boards"
    )


# =========================================
# GAME QUICK PICKS
# =========================================

def lotto_quickpick():

    return ",".join(
        map(
            str,
            sorted(
                random.sample(
                    range(1, 53),
                    6
                )
            )
        )
    )


def powerball_quickpick():

    main_numbers = sorted(
        random.sample(
            range(1, 51),
            5
        )
    )

    powerball = random.randint(
        1,
        20
    )

    return (
        ",".join(
            map(
                str,
                main_numbers
            )
        )
        + ","
        + str(powerball)
    )


def daily_lotto_quickpick():

    return ",".join(
        map(
            str,
            sorted(
                random.sample(
                    range(1, 37),
                    5
                )
            )
        )
    )

def compare_numbers(ticket_numbers, winning_numbers):

    # =========================================================
    # NORMALISE TICKET NUMBERS
    # =========================================================

    if isinstance(ticket_numbers, str):

        ticket = {
            str(num).strip()
            for num in ticket_numbers.split(",")
            if str(num).strip()
        }

    elif isinstance(ticket_numbers, (list, tuple, set)):

        ticket = {
            str(num).strip()
            for num in ticket_numbers
            if str(num).strip()
        }

    else:

        ticket = {
            str(ticket_numbers).strip()
        }


    # =========================================================
    # NORMALISE WINNING NUMBERS
    # =========================================================

    if isinstance(winning_numbers, str):

        winning = {
            str(num).strip()
            for num in winning_numbers.split(",")
            if str(num).strip()
        }

    elif isinstance(winning_numbers, (list, tuple, set)):

        winning = {
            str(num).strip()
            for num in winning_numbers
            if str(num).strip()
        }

    else:

        winning = {
            str(winning_numbers).strip()
        }


    # =========================================================
    # REMOVE POWERBALL BONUS FROM MAIN NUMBER COMPARISON
    # =========================================================

    winning = {
        number
        for number in winning
        if not number.upper().startswith("PB:")
    }


    # =========================================================
    # FIND MATCHES
    # =========================================================

    matches = ticket.intersection(winning)


    # =========================================================
    # CONVERT MATCHES BACK TO INTEGERS
    # =========================================================

    cleaned_matches = []

    for number in matches:

        try:

            cleaned_matches.append(
                int(number)
            )

        except ValueError:

            cleaned_matches.append(
                number
            )


    # =========================================================
    # SORT SAFELY
    # =========================================================

    try:

        cleaned_matches = sorted(
            cleaned_matches,
            key=lambda x: int(x)
        )

    except (ValueError, TypeError):

        cleaned_matches = sorted(
            cleaned_matches,
            key=str
        )


    return (
        len(cleaned_matches),
        cleaned_matches
    )

GAME_PRICES_PER_BOARD = {
    "Lotto": 6,
    "Lotto Plus": 6,
    "PowerBall": 5,
    "PowerBall Plus": 5,
    "Daily Lotto": 3
}


def get_game_price(game):
    return GAME_PRICES_PER_BOARD.get(game, 0)

MPG_VAS_FEE_PER_BOARD = 1


def calculate_pricing(game, boards):
    ticket_value = boards * get_game_price(game)
    service_fee = boards * MPG_VAS_FEE_PER_BOARD
    total_charged = ticket_value + service_fee

    return ticket_value, service_fee, total_charged


def validate_manual_numbers(game, value):
    """
    Validate customer-entered lottery numbers before a ticket is created.

    Returns:
        (True, cleaned_numbers, "")
        or
        (False, None, error_message)
    """

    if value is None:
        return False, None, "Numbers are required."

    raw = str(value).strip()

    if not raw:
        return False, None, "Numbers are required."

    parts = [item.strip() for item in raw.split(",")]

    if any(not item for item in parts):
        return False, None, "Use numbers separated by commas."

    if any(not item.isdigit() for item in parts):
        return False, None, "Numbers must contain digits only."

    numbers = [int(item) for item in parts]

    if game in ["Lotto", "Lotto Plus"]:
        if len(numbers) != 6:
            return False, None, "Enter exactly 6 numbers."

        if len(set(numbers)) != 6:
            return False, None, "Numbers cannot be repeated."

        if any(number < 1 or number > 52 for number in numbers):
            return False, None, "Numbers must be between 1 and 52."

        cleaned = ",".join(map(str, sorted(numbers)))
        return True, cleaned, ""

    if game in ["PowerBall", "PowerBall Plus"]:
        if len(numbers) != 6:
            return False, None, "Enter 5 numbers plus PowerBall."

        main_numbers = numbers[:5]
        powerball = numbers[5]

        if len(set(main_numbers)) != 5:
            return False, None, "Main numbers cannot be repeated."

        if any(number < 1 or number > 50 for number in main_numbers):
            return False, None, "Main numbers must be between 1 and 50."

        if powerball < 1 or powerball > 20:
            return False, None, "PowerBall must be between 1 and 20."

        cleaned = ",".join(map(str, sorted(main_numbers) + [powerball]))
        return True, cleaned, ""

    if game == "Daily Lotto":
        if len(numbers) != 5:
            return False, None, "Enter exactly 5 numbers."

        if len(set(numbers)) != 5:
            return False, None, "Numbers cannot be repeated."

        if any(number < 1 or number > 36 for number in numbers):
            return False, None, "Numbers must be between 1 and 36."

        cleaned = ",".join(map(str, sorted(numbers)))
        return True, cleaned, ""

    return False, None, "Invalid game."

def calculate_prize(game, matches):

    prizes = {

        "Lotto": {
            3: 50,
            4: 250,
            5: 5000,
            6: 5000000
        },

        "PowerBall": {
            3: 50,
            4: 300,
            5: 10000,
            6: 10000000
        },

        "PowerBall Plus": {
            3: 50,
            4: 300,
            5: 10000,
            6: 2000000
        },

        "Daily Lotto": {
            2: 10,
            3: 50,
            4: 300,
            5: 3000
        }

    }

    return prizes.get(game, {}).get(matches, 0)

def check_ticket(ticket_data, winning_numbers, powerball_number=None, divisions=None):

    game = ticket_data["game"]

    # =========================================================
    # POWERBALL / POWERBALL PLUS
    # =========================================================

    if game in ["PowerBall", "PowerBall Plus"]:

        ticket_numbers = ticket_data["numbers"]

        # -----------------------------------------
        # Normalise ticket numbers
        # -----------------------------------------

        if isinstance(ticket_numbers, str):

            ticket_numbers = [
                str(number).strip()
                for number in ticket_numbers.split(",")
                if str(number).strip()
            ]

        else:

            ticket_numbers = [
                str(number).strip()
                for number in ticket_numbers
                if str(number).strip()
            ]

        # -----------------------------------------
        # MPG PowerBall format:
        #
        # First 5 = main numbers
        # 6th      = PowerBall
        # -----------------------------------------

        main_ticket_numbers = ticket_numbers[:5]

        ticket_powerball = (
            ticket_numbers[5]
            if len(ticket_numbers) >= 6
            else None
        )

        # -----------------------------------------
        # Compare the 5 main numbers
        # -----------------------------------------

        main_matches, matched = compare_numbers(
            main_ticket_numbers,
            winning_numbers
        )

        # -----------------------------------------
        # Check PowerBall separately
        # -----------------------------------------

        powerball_match = False

        if (
            powerball_number is not None
            and ticket_powerball is not None
        ):

            powerball_match = (
                str(ticket_powerball).strip()
                ==
                str(powerball_number).strip()
            )

        # -----------------------------------------
        # ResultsZA OFFICIAL PRIZE LOGIC
        # -----------------------------------------
        #
        # The payout comes from the official
        # division for this specific draw.
        #
        # Example:
        # MATCH 4 + PowerBall
        # MATCH 4
        # MATCH PowerBall
        #
        # We do NOT use hard-coded prize amounts.
        # -----------------------------------------

        prize = 0
        winning_division = None

        if divisions is None:
            divisions = []

        if main_matches == 5 and powerball_match:
            target_match = "MATCH 5 + POWERBALL"

        elif main_matches == 5:
            target_match = "MATCH 5"

        elif main_matches == 4 and powerball_match:
            target_match = "MATCH 4 + POWERBALL"

        elif main_matches == 4:
            target_match = "MATCH 4"

        elif main_matches == 3 and powerball_match:
            target_match = "MATCH 3 + POWERBALL"

        elif main_matches == 3:
            target_match = "MATCH 3"

        elif main_matches == 2 and powerball_match:
            target_match = "MATCH 2 + POWERBALL"

        elif main_matches == 1 and powerball_match:
            target_match = "MATCH 1 + POWERBALL"

        elif main_matches == 0 and powerball_match:
            target_match = "MATCH POWERBALL"

        else:
            target_match = None

        if target_match:

            for division in divisions:

                division_match = str(
                    division.get("match", "")
                ).strip().upper()

                if division_match == target_match:

                    winning_division = division

                    try:
                        prize = float(
                            division.get(
                                "winning_amount",
                                0
                            )
                        )
                    except (TypeError, ValueError):
                        prize = 0

                    break

        # -----------------------------------------
        # Add PowerBall to matched numbers
        # -----------------------------------------

        matched_numbers = list(matched)

        if powerball_match:
            matched_numbers.append(
                f"PB:{ticket_powerball}"
            )

        return {
            "matches": main_matches,
            "matched_numbers": matched_numbers,
            "powerball_match": powerball_match,
            "winning_division": winning_division,
            "won": prize > 0,
            "winnings": prize
        }

    # =========================================================
    # LOTTO / DAILY LOTTO
    # =========================================================

    matches, matched = compare_numbers(
        ticket_data["numbers"],
        winning_numbers
    )

    if game == "Daily Lotto" and divisions:
        target_match = f"MATCH {matches}"
        prize = 0

        for division in divisions:
            division_match = str(
                division.get("match", "")
            ).strip().upper()

            if division_match == target_match:
                try:
                    prize = float(
                        division.get("winning_amount", 0)
                    )
                except (TypeError, ValueError):
                    prize = 0
                break
    else:
        prize = calculate_prize(
            game,
            matches
        )

    return {
        "matches": matches,
        "matched_numbers": matched,
        "powerball_match": False,
        "won": prize > 0,
        "winnings": prize
    }

# ==========================================
# SAVE SMS LOG
# ==========================================

def save_sms_log(reference, phone, sms_type, status, message):

    print("========== SAVING SMS LOG ==========")
    print(reference)
    print(phone)
    print(sms_type)

    db.collection("sms_logs").add({

        "reference": reference,
        "phone": phone,
        "message_type": sms_type,
        "status": status,
        "message": message,
        "sent_at": datetime.now()

    })

    print("========== SMS LOG SAVED ==========")

# =========================================
# RESULTSZA → MPG FIRESTORE RESULT SYNC
# =========================================

def sync_resultsza_result(game_name):

    print("\n" + "=" * 60)
    print("RESULTSZA → MPG FIRESTORE SYNC")
    print("=" * 60)

    print("Requested Game:", game_name)

    # =========================================
    # GET OFFICIAL RESULT FROM RESULTSZA
    # =========================================

    result = get_firestore_result(game_name)

    print("ResultsZA Result:")
    print(result)

    if not result:
        print("❌ No result returned from ResultsZA.")
        return None

    # =========================================
    # EXTRACT OFFICIAL RESULT
    # =========================================

    draw_id = str(
        result.get("draw_id", "")
    ).strip()

    game = result.get(
        "game",
        game_name
    )

    draw_numbers = result.get(
        "draw_numbers",
        []
    )

    bonus_ball = result.get(
        "bonus_ball"
    )

    powerball = result.get(
        "powerball"
    )

    draw_date = result.get(
        "draw_date"
    )

    if not draw_id:

        print(
            "❌ ResultsZA result has no draw_id."
        )

        return None

    if not draw_numbers:

        print(
            "❌ ResultsZA result has no draw_numbers."
        )

        return None

    # =========================================
    # NORMALISE GAME NAME
    # =========================================

    if game.lower() == "powerball":

        game = "PowerBall"

    elif game.lower() in [
        "powerball plus",
        "powerball xtra"
    ]:

        game = "PowerBall Plus"

    elif game.lower() == "daily lotto":

        game = "Daily Lotto"

    elif game.lower() == "lotto":

        game = "Lotto"

    elif game.lower() in [
        "lotto plus",
        "lotto plus 1"
    ]:

        game = "Lotto Plus"

    # =========================================
    # FIRESTORE RESULT DOCUMENT ID
    # =========================================

    document_id = (
        f"{draw_id}_"
        f"{game.upper().replace(' ', '')}"
    )

    print(
        "Firestore Result Document:",
        document_id
    )

    # =========================================
    # PREPARE OFFICIAL MPG RESULT
    # =========================================

    result_data = {

        "game": game,

        "draw_id": draw_id,

        "draw_date": draw_date,

        # =====================================
        # INTERNAL MPG RESULT FIELDS
        # =====================================

        "numbers": [
            str(number)
            for number in draw_numbers
        ],

        "bonus_number": (
            str(bonus_ball)
            if bonus_ball is not None
            else None
        ),

        # =====================================
        # OFFICIAL RESULTSZA FIELDS
        # =====================================

        "official_draw_numbers": [
            str(number)
            for number in draw_numbers
        ],

        "official_bonus_number": (
            str(bonus_ball)
            if bonus_ball is not None
            else None
        ),

        "source": "ResultsZA",

        # =====================================
        # POWERBALL
        # =====================================

        "powerball": (
            str(powerball)
            if powerball is not None
            else None
        ),

        # =====================================
        # RESULT VERIFICATION
        # =====================================

        "result_checked": False,

        "verification_status": "PENDING",

        "processed_at": None,

        # =====================================
        # MATCH INFORMATION
        # =====================================

        "matched_numbers": [],

        "matches": 0,

        # =====================================
        # PROCESSING
        # =====================================

        "processed": False,

        "divisions": result.get("divisions", []),

        "status": "PENDING"

    }

    # =========================================
    # SAVE OFFICIAL RESULT
    # =========================================

    db.collection("results").document(
        document_id
    ).set(
        result_data,
        merge=True
    )

    print(
        "\n✅ ResultsZA result saved to Firestore."
    )

    # =========================================
    # ATTACH OFFICIAL DRAW ID TO TICKETS
    # AND PROCESS THEM
    # =========================================

    print("\n" + "=" * 60)
    print("LINKING MPG TICKETS TO OFFICIAL DRAW")
    print("=" * 60)

    print("Game:", game)
    print("Draw Date:", draw_date)
    print("Official Draw ID:", draw_id)

    # -----------------------------------------
    # Extract calendar date from ResultsZA
    # -----------------------------------------

    official_date = None

    if draw_date:

        try:
            official_date = str(draw_date)[:10]

        except Exception:
            official_date = None

    print(
        "Official Calendar Date:",
        official_date
    )

    linked_count = 0

    if official_date:

        # -----------------------------------------
        # Find MPG tickets belonging to this
        # calendar draw date.
        # -----------------------------------------

        tickets_query = (
            db.collection("tickets")
            .where(
                filter=FieldFilter(
                    "game",
                    "==",
                    game
                )
            )
            .where(
                filter=FieldFilter(
                    "draw_date_key",
                    "==",
                    official_date
                )
            )
            .stream()
        )

        tickets_for_draw = list(
            tickets_query
        )

        print(
            "Potential tickets found:",
            len(tickets_for_draw)
        )

        # -----------------------------------------
        # LINK EACH TICKET TO OFFICIAL DRAW
        # -----------------------------------------

        for ticket_doc in tickets_for_draw:

            ticket_data = ticket_doc.to_dict()

            reference = ticket_data.get(
                "reference",
                ticket_doc.id
            )

            existing_official_id = str(
                ticket_data.get(
                    "official_draw_id",
                    ""
                )
            ).strip()

            # -----------------------------------------
            # Already linked to this exact draw
            # -----------------------------------------

            if existing_official_id == draw_id:

                print(
                    "ℹ️ Ticket already linked:",
                    reference,
                    "→",
                    draw_id
                )

                continue

            # -----------------------------------------
            # Ticket is linked to another official draw
            # -----------------------------------------

            if existing_official_id:

                print(
                    "⚠️ Ticket already linked "
                    "to another draw:",
                    reference,
                    existing_official_id
                )

                continue

            # -----------------------------------------
            # Attach official draw information
            # -----------------------------------------

            ticket_doc.reference.update({

                "draw_id": draw_id,

                "official_draw_id": draw_id,

                "official_draw_date": draw_date

            })

            linked_count += 1

            print(
                "✅ Ticket linked:",
                reference,
                "→",
                draw_id
            )

    print(
        "\nTickets linked to official draw:",
        linked_count
    )

    print("=" * 60)

    # =========================================
    # AUTOMATICALLY PROCESS THE OFFICIAL DRAW
    # =========================================

    print("\n" + "=" * 60)
    print("AUTOMATIC DRAW PROCESSING")
    print("=" * 60)

    print(
        "Processing official draw:",
        draw_id,
        game
    )

    try:

        process_draw_results(
            draw_id,
            game
        )

        print(
            "✅ Automatic draw processing completed."
        )

    except Exception as e:

        print(
            "❌ Automatic draw processing failed:",
            str(e)
        )

    print("=" * 60)

    # =========================================
    # FINAL OFFICIAL RESULT DISPLAY
    # =========================================

    print("\n" + "=" * 60)
    print("OFFICIAL MPG RESULT")
    print("=" * 60)

    print("GAME:", game)
    print("DRAW ID:", draw_id)
    print("DRAW DATE:", draw_date)
    print(
        "NUMBERS:",
        result_data["numbers"]
    )
    print(
        "BONUS:",
        result_data["bonus_number"]
    )
    print(
        "POWERBALL:",
        result_data["powerball"]
    )
    print(
        "SOURCE:",
        "ResultsZA"
    )
    print(
        "DOCUMENT:",
        document_id
    )
    print(
        "TICKETS LINKED:",
        linked_count
    )

    print("=" * 60)

    return result_data

def get_draw_result(draw_id, game):

    doc_id = f"{draw_id}_{game.upper().replace(' ', '')}"

    doc = db.collection("results").document(doc_id).get()

    if not doc.exists:

        return None

    return doc.to_dict()

def process_draw_results(draw_id, game):

    print(f"\nProcessing {game} draw {draw_id}")

    # ==========================================
    # GET STORED DRAW RESULT
    # ==========================================

    draw_result = get_draw_result(draw_id, game)

    print("Draw Result:")
    print(draw_result)

    if not draw_result:

        print("❌ Draw result not found.")

        return

    # ==========================================
    # GET WINNING NUMBERS
    # ==========================================

    winning_numbers = draw_result.get("draw_numbers")

    if not winning_numbers:

        winning_numbers = draw_result.get("numbers")

    # ==========================================
    # BACKWARD COMPATIBILITY
    # ==========================================
    # Older PowerBall documents used:
    #
    # "numbers "
    #
    # with a trailing space.
    # ==========================================

    if not winning_numbers:

        winning_numbers = draw_result.get("numbers ")

    if not winning_numbers:

        print("❌ Winning numbers not found.")

        return

    print("Raw Winning Numbers:")
    print(winning_numbers)

    # ==========================================
    # NORMALISE WINNING NUMBERS
    # ==========================================

    if isinstance(winning_numbers, str):

        winning_numbers = [
            number.strip()
            for number in winning_numbers.split(",")
            if number.strip()
        ]

    else:

        winning_numbers = [
            str(number).strip()
            for number in winning_numbers
            if str(number).strip()
        ]

    # ==========================================
    # POWERBALL NORMALISATION
    # ==========================================

    # ==========================================
    # POWERBALL NORMALISATION
    # ==========================================

    if game in ["PowerBall", "PowerBall Plus"]:

        # ResultsZA stores the PowerBall separately
        bonus_number = draw_result.get("powerball")

        if bonus_number is not None:

            bonus_number = str(
                bonus_number
            ).strip()

        cleaned_numbers = []

        for number in winning_numbers:

            number = str(number).strip()

            # Support older stored PB:XX format too
            if number.upper().startswith("PB:"):

                bonus_number = number.split(
                    ":",
                    1
                )[1].strip()

            else:

                cleaned_numbers.append(number)

        winning_numbers = cleaned_numbers

    else:

        # Lotto / Daily Lotto bonus handling
        bonus_number = draw_result.get(
            "bonus_number"
        )

    # ==========================================
    # FINAL RESULT
    # ==========================================

    print("Final Winning Numbers:")
    print(winning_numbers)

    print("PowerBall / Bonus Number:")
    print(bonus_number)

    draw_ref = db.collection("draws").document(draw_id)

    draw_data = {
        "draw_id": draw_id,
        "game": game,
        "draw_date": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "winning_numbers": winning_numbers,

        "bonus_number": bonus_number,

        "status": "Processing",

        "processed": False,

        "processed_at": "",

        "tickets_processed": 0,

        "winner_count": 0,

        "loser_count": 0,

        "total_prize_paid": 0
    }

    draw_ref.set(draw_data, merge=True)

    print("=" * 60)
    print("Looking for tickets")
    print("Draw ID:", draw_id)
    print("Game:", game)
    print("=" * 60)

    tickets = (
        tickets_ref
            .where(
                filter=FieldFilter(
                    "official_draw_id",
                    "==",
                    draw_id
                )
            )
            .where(
                filter=FieldFilter(
                    "game",
                    "==",
                    game
                )
            )
            .stream()
    )
    tickets = list(tickets)

    print("Tickets Found:", len(tickets))

    for t in tickets:
        print(t.to_dict())

    winner_count = 0
    loser_count = 0
    total_prize = 0
    ticket_count = 0

    for ticket in tickets:

        ticket_data = ticket.to_dict()

        # Handle tickets that have already been processed
        # without processing them again.
        if ticket_data.get("result_checked"):
            print("=" * 60)
            print("Ticket already processed. Using existing result.")
            print(ticket_data["reference"])
            print("=" * 60)

            ticket_count += 1

            if ticket_data.get("won"):
                winner_count += 1
                total_prize += ticket_data.get("winnings", 0) or 0
            else:
                loser_count += 1

            continue

        print("Ticket Found:")
        print(ticket_data["reference"])
        print(ticket_data["game"])
        print(ticket_data["draw_id"])

        result = check_ticket(
            ticket_data,
            winning_numbers,
            draw_result.get("powerball") if game in ["PowerBall", "PowerBall Plus"] else bonus_number,
            draw_result.get("divisions", [])
        )

        ticket_count += 1

        if result["won"]:
            winner_count += 1
            total_prize += result["winnings"]
        else:
            loser_count += 1

        print(ticket.id)
        print(result)

        print("=" * 60)
        print("ABOUT TO UPDATE FIRESTORE")
        print("Ticket:", ticket_data["reference"])
        print("Winning Numbers:", winning_numbers)
        print("Processed At:", datetime.now().strftime("%d/%m/%Y %H:%M:%S"))
        print("=" * 60)

        print("STARTING UPDATE")

        processed_timestamp = datetime.now().strftime(
    "%d/%m/%Y %H:%M:%S"
)

        ticket.reference.update({

            # =========================================
            # TICKET RESULT
            # =========================================

            "matches": result["matches"],

            "matched_numbers": result["matched_numbers"],

            "won": result["won"],

            "winnings": result["winnings"],

            # =========================================
            # RESULT VERIFICATION
            # =========================================

            "result_checked": True,

            "verification_status": "VERIFIED",

            "draw_numbers": winning_numbers,

            "processed_at": processed_timestamp,

            # =========================================
            # OFFICIAL RESULT
            #
            # For a normal/current draw, the result
            # being processed is the official result.
            # =========================================

            "official_draw_numbers": winning_numbers,

            "official_matched_numbers": result[
                "matched_numbers"
            ],

            "official_matches": result[
                "matches"
            ],

            # =========================================
            # OFFICIAL BONUS
            #
            # Only save this if get_draw_result()
            # actually provides a bonus number.
            # =========================================

            "official_bonus_number": bonus_number,

            # =========================================
            # PAYMENT / SETTLEMENT
            # =========================================

            "payment_status": (
                "AWAITING SIZEKHAYA SETTLEMENT"
                if result["won"]
                else "NOT APPLICABLE"
            )

        })
        
        print("UPDATE FINISHED")

        updated_ticket = ticket.reference.get()

        print("=" * 60)
        print("UPDATED TICKET FROM FIRESTORE")
        print(updated_ticket.to_dict())
        print("=" * 60)
        
        # =========================================
        # SEND SMS RESULT
        # =========================================

        phone = ticket_data.get("phone")

        print("Phone:", phone)
        print("Username:", AT_USERNAME)

        try:

            # =========================================
            # WINNER SMS
            # =========================================

            if result["won"]:

                print("Attempting WINNER SMS...")

                sms_message = (
                    f"MPG: Congratulations! Your ticket "
                    f"{ticket_data['reference']} won "
                    f"R{result['winnings']}. "
                    f"Prize payment will be processed."
                )

                response = send_sms_direct(
                    phone,
                    sms_message
                )

                print("Direct SMS response:", response)

                # -----------------------------------------
                # CHECK AFRICA'S TALKING RESPONSE
                # -----------------------------------------

                sms_success = False
                message_id = None
                recipient_status = None

                try:

                    response_data = json.loads(
                        response["response"]
                    )

                    recipients = (
                        response_data
                        .get("SMSMessageData", {})
                        .get("Recipients", [])
                    )

                    if recipients:

                        recipient = recipients[0]

                        recipient_status = recipient.get(
                            "status"
                        )

                        message_id = recipient.get(
                            "messageId"
                        )

                        sms_success = (
                            response["status_code"] == 201
                            and recipient_status == "Success"
                        )

                except Exception as parse_error:

                    print(
                        "SMS response parsing failed:",
                        parse_error
                    )

                # -----------------------------------------
                # SAVE SMS LOG
                # -----------------------------------------

                save_sms_log(
                    ticket_data["reference"],
                    phone,
                    "Winner",
                    "Delivered" if sms_success else "Failed",
                    sms_message
                )

                # -----------------------------------------
                # UPDATE TICKET ONLY IF SMS SUCCEEDED
                # -----------------------------------------

                ticket.reference.update({

                    "sms_sent": sms_success,

                    "sms_status": (
                        "Delivered"
                        if sms_success
                        else "Failed"
                    ),

                    "sms_message_id": message_id,

                    "sms_sent_at": (
                        datetime.utcnow()
                        if sms_success
                        else None
                    )

                })

                print(
                    "Winner SMS status:",
                    "Delivered"
                    if sms_success
                    else "Failed"
                )

                print(
                    "Winner SMS message ID:",
                    message_id
                )

            # =========================================
            # LOSER / RESULT SMS
            # =========================================

            else:

                print("Attempting LOSER SMS...")

                sms_message = (
                    f"MPG: Your ticket "
                    f"{ticket_data['reference']} "
                    f"did not win this draw. "
                    f"Thank you for playing."
                )

                response = send_sms_direct(
                    phone,
                    sms_message
                )

                print("Direct SMS response:", response)

                # -----------------------------------------
                # CHECK AFRICA'S TALKING RESPONSE
                # -----------------------------------------

                sms_success = False
                message_id = None
                recipient_status = None

                try:

                    response_data = json.loads(
                        response["response"]
                    )

                    recipients = (
                        response_data
                        .get("SMSMessageData", {})
                        .get("Recipients", [])
                    )

                    if recipients:

                        recipient = recipients[0]

                        recipient_status = recipient.get(
                            "status"
                        )

                        message_id = recipient.get(
                            "messageId"
                        )

                        sms_success = (
                            response["status_code"] == 201
                            and recipient_status == "Success"
                        )

                except Exception as parse_error:

                    print(
                        "SMS response parsing failed:",
                        parse_error
                    )

                # -----------------------------------------
                # SAVE SMS LOG
                # -----------------------------------------

                save_sms_log(

                    ticket_data["reference"],
                    phone,
                    "Results",
                    "Delivered" if sms_success else "Failed",
                    sms_message

                )

                # -----------------------------------------
                # UPDATE TICKET ONLY IF SMS SUCCEEDED
                # -----------------------------------------

                ticket.reference.update({

                    "sms_sent": sms_success,

                    "sms_status": (
                        "Delivered"
                        if sms_success
                        else "Failed"
                    ),

                    "sms_message_id": message_id,

                    "sms_sent_at": (
                        datetime.utcnow()
                        if sms_success
                        else None
                    )

                })

                print(
                    "Loser SMS status:",
                    "Delivered"
                    if sms_success
                    else "Failed"
                )

                print(
                    "Loser SMS message ID:",
                    message_id
                )

            print("Africa's Talking Response:")
            print(response)

        except Exception as e:

            print("=" * 50)
            print("SMS FAILED")
            print(type(e))
            print(e)
            print("=" * 50)

            # -----------------------------------------
            # IMPORTANT:
            # Do NOT mark sms_sent=True when sending
            # fails.
            # -----------------------------------------

            try:

                ticket.reference.update({

                    "sms_sent": False,

                    "sms_status": "Failed"

                })

            except Exception as update_error:

                print(
                    "Could not update SMS failure status:",
                    update_error
                )

        print(
            f"✅ {ticket_data['reference']} | Won: {result['won']} | Prize: R{result['winnings']}"
        )

    draws_ref = db.collection("results")

    document = f"{draw_id}_{game.upper().replace(' ','')}"

    draws_ref.document(document).update({

        "processed": True,

        "status": "Completed",

        "result_checked": True,

        "verification_status": "VERIFIED",

        "processed_at": datetime.now().strftime(
            "%d/%m/%Y %H:%M:%S"
        ),

        "tickets_processed": ticket_count,

        "winner_count": winner_count,

        "loser_count": loser_count,

        "total_prize_paid": total_prize

    })

    # =========================================================
    # UPDATE DRAW RECORD
    # =========================================================

    draw_ref.update({

        "status": "Completed",

        "processed": True,

        "processed_at": (
            datetime.now().strftime(
                "%d/%m/%Y %H:%M:%S"
            )
        ),

        "tickets_processed": ticket_count,

        "winner_count": winner_count,

        "loser_count": loser_count,

        "total_prize_paid": total_prize

    })

    print("=" * 60)
    print("DRAW SUMMARY")
    print("=" * 60)
    print("Game              :", game)
    print("Draw ID           :", draw_id)
    print("Tickets Processed :", ticket_count)
    print("Winners           :", winner_count)
    print("Losers            :", loser_count)
    print("Total Prize       : R", total_prize)
    print("=" * 60)

    (f"✅ {game} draw {draw_id} marked as processed.")

    print(f"✅ {game} draw {draw_id} marked as processed.")
# =========================================
# SAVE TICKET
# =========================================
def save_ticket(
    phone,
    game,
    boards,
    cost,
    ref,
    numbers
):

    try:

        # =========================================
        # DETERMINE INTENDED DRAW
        # =========================================

        draw_date_key = get_next_draw_date(game)

        ticket = {

            "phone": phone,
            "game": game,
            "boards": boards,
            "cost": cost,
            "reference": ref,
            "numbers": numbers,
            "played_at": current_date(),

            "draw": draw_details(game),

            # =========================================
            # INTENDED DRAW
            # =========================================
            # Calendar date of the draw this ticket
            # belongs to.

            "draw_date_key": draw_date_key,

            # =========================================
            # OFFICIAL DRAW
            # =========================================
            # ResultsZA attaches these after the official
            # result is imported.

            "draw_id": "",
            "official_draw_id": "",
            "official_draw_date": "",

            # =========================================
            # PAYMENT
            # =========================================

            "payment_status": "PENDING",

            # =========================================
            # RESULT VERIFICATION
            # =========================================

            "result_checked": False,
            "verification_status": "PENDING",
            "processed_at": "",

            # =========================================
            # RESULT DATA
            # =========================================

            "draw_numbers": [],
            "matched_numbers": [],
            "matches": 0,

            # =========================================
            # TICKET RESULT
            # =========================================

            "status": "ACTIVE",
            "won": False,
            "winnings": 0
        }

        # =========================================
        # SAVE TICKET
        # =========================================

        if tickets_ref:

            tickets_ref.document(ref).set(
                ticket
            )

        # =========================================
        # FINANCIAL TRANSACTION LEDGER
        # =========================================

        ticket_value, service_fee, total_charged = calculate_pricing(
            game,
            boards
        )

        transaction = {

            "transaction_reference": ref,
            "ticket_reference": ref,

            "phone": phone,
            "game": game,

            "ticket_value": float(ticket_value),
            "service_fee": float(service_fee),
            "total_charged": float(total_charged),
            "pricing_status": "PROPOSED - PENDING EXTERNAL AGREEMENT",

            "currency": "ZAR",

            "channel": "USSD",
            "billing_provider": "Vodacom",

            "status": "RECORDED",

            "created_at": current_date()

        }

        db.collection("transactions").document(ref).set(
            transaction
        )

        print(
            f"💰 Transaction recorded: {ref} | "
            f"Ticket Value: R{float(ticket_value):.2f} | "
            f"Service Fee: R{float(service_fee):.2f} | "
            f"Total Charged: R{float(total_charged):.2f}"
        )

        print("\n" + "=" * 50)
        print("🎟️ MPG TICKET")
        print("=" * 50)

        for key, value in ticket.items():

            print(f"{key}: {value}")

        print("=" * 50 + "\n")

    except Exception as e:

        print(
            "❌ Ticket save error:",
            str(e)
        )

def save_ussd_session(
    session_id,
    phone,
    game,
    boards,
    cost,
    numbers
):

    db.collection("ussd_sessions").document(
        session_id
    ).set({

        "session_id": session_id,
        "phone": phone,
        "game": game,
        "boards": boards,
        "cost": cost,
        "numbers": numbers,
        "created_at": datetime.now().isoformat()

    })


def get_ussd_session(session_id):

    doc = (
        db.collection("ussd_sessions")
        .document(session_id)
        .get()
    )

    if doc.exists:
        return doc.to_dict()

    return None


def delete_ussd_session(session_id):

    (
        db.collection("ussd_sessions")
        .document(session_id)
        .delete()
    )

# =========================================
# COMPLETE TICKET
# =========================================
def complete_ticket(
    phone,
    game,
    boards,
    cost,
    numbers
):

    ref = receipt()

    save_ticket(
        phone=phone,
        game=game,
        boards=boards,
        cost=cost,
        ref=ref,
        numbers=numbers
    )

    try:

        process_ticket(
            phone=phone,
            game=game,
            boards=boards,
            cost=cost,
            ref=ref,
            numbers=numbers
        )

    except Exception as e:

        print(
            "⚠️ Worker error:",
            str(e)
        )

    return ref

# =========================================
# USSD
# =========================================
@app.route("/sms", methods=["POST"])
def sms_callback():

    print("SMS CALLBACK RECEIVED")
    print(request.form)

    return "OK", 200

@app.route("/ussd",methods=["POST"])
def ussd():

    print("METHOD:", request.method)
    print("FORM:", request.form)
    print("ARGS:", request.args)

    try:

        text = request.values.get(
            "text",
            ""
        ).strip()

        phone = request.values.get(
            "phoneNumber",
            ""
        ).strip()

        session_id = request.values.get(
            "sessionId",
            ""
        ).strip()

        parts = (
            text.split("*")
            if text else []
        )

        print(
            "📥",
            phone,
            text
        )

        # =========================================
        # MAIN MENU
        # =========================================
        if text == "":

            return ussd_response(
                "CON MPG LOTTO\n"
                "18+ ONLY\n"
                "1.Lotto\n"
                "2.PowerBall\n"
                "3.Daily Lotto\n"
                "4.Results\n"
                "5.My Tickets"
            )

        # =========================================
        # LOTTO
        # =========================================
        if parts[0] == "1":

            # GAME TYPE
            if len(parts) == 1:

                return ussd_response(
                    "CON Select\n"
                    "1.Lotto\n"
                    "2.Lotto Plus"
                )

            if parts[1] not in [
                "1",
                "2"
            ]:

                return ussd_response(
                    "END Invalid Option"
                )

            game = (
                "Lotto"
                if parts[1] == "1"
                else "Lotto Plus"
            )

            # BOARDS
            if len(parts) == 2:

                return ussd_response(
                    boards_menu()
                )

            if not parts[2].isdigit():

                return ussd_response(
                    "END Invalid Boards"
                )

            boards = int(parts[2])

            if boards < 1 or boards > 5:

                return ussd_response(
                    "END 1-5 Boards Only"
                )

            cost = boards * get_game_price(game)

            ticket_value, service_fee, total_charge = calculate_pricing(
                game,
                boards
            )

            # PLAY TYPE
            if len(parts) == 3:

                return ussd_response(
                    f"CON R{total_charge} Airtime\n"
                    "1.Quick Pick\n"
                    "2.Manual"
                )

            if parts[3] not in [
                "1",
                "2"
            ]:

                return ussd_response(
                    "END Invalid"
                )

            # QUICK PICK
            if parts[3] == "1":

                # Generate numbers only once
                if len(parts) == 4:

                    numbers = lotto_quickpick()

                    save_ussd_session(
                        session_id=session_id,
                        phone=phone,
                        game=game,
                        boards=boards,
                        cost=cost,
                        numbers=numbers
                    )

                    return ussd_response(
                        f"CON {game}\n"
                        f"Nums:{numbers}\n"
                        f"R{total_charge} Airtime\n"
                        "1.Confirm\n"
                        "2.Cancel"
                    )

                # Confirmation
                if len(parts) == 5:

                    if parts[4] == "2":

                        delete_ussd_session(
                            session_id
                        )

                        return ussd_response(
                            "END Cancelled"
                        )

                    if parts[3] != "1":

                        return ussd_response(
                            "CON Invalid option\n"
                            "1.Confirm\n"
                            "2.Cancel"
                        )

                    if parts[4] != "1":

                        return ussd_response(
                            "CON Invalid option\n"
                            "1.Confirm\n"
                            "2.Cancel"
                        )

                    pending = get_ussd_session(
                        session_id
                    )

                    if not pending:

                        return ussd_response(
                            "END Session expired. Please try again."
                        )

                    numbers = pending.get(
                        "numbers",
                        ""
                    )

                    ref = complete_ticket(
                        phone=pending.get("phone", phone),
                        game=pending.get("game", game),
                        boards=pending.get("boards", boards),
                        cost=pending.get("cost", cost),
                        numbers=numbers
                    )

                    delete_ussd_session(
                        session_id
                    )

                    return ussd_response(
                        f"END Ticket OK\n"
                        f"Ref:{ref}\n"
                        f"{draw_details(game)}"
                    )
    
                    ref = complete_ticket(
                        phone=phone,
                        game=game,
                        boards=boards,
                        cost=cost,
                        numbers=numbers
                    )

                    return ussd_response(
                        f"END Ticket OK\n"
                        f"Ref:{ref}\n"
                        f"{draw_details(game)}"
                    )

            # MANUAL
            if parts[3] == "2":

                # Ask customer for numbers
                if len(parts) == 4:
                    return ussd_response(
                        "CON Enter 6 nums\n"
                        "Example: 5,12,18,21,33,41"
                    )

                # Validate customer-entered numbers
                if len(parts) == 5:
                    numbers = parts[4]

                    valid, cleaned_numbers, error_message = validate_manual_numbers(
                        game,
                        numbers
                    )

                    if not valid:
                        return ussd_response(
                            f"CON Invalid numbers: {error_message}\n"
                            "Please try again."
                        )

                    return ussd_response(
                        f"CON Confirm\n"
                        f"Nums:{cleaned_numbers}\n"
                        f"R{total_charge} Airtime\n"
                        "1.Yes\n"
                        "2.No"
                    )

                # Customer confirmation
                if len(parts) == 6:

                    if parts[5] == "2":
                        delete_ussd_session(session_id)
                        return ussd_response(
                            "END Cancelled"
                        )

                    if parts[5] != "1":
                        return ussd_response(
                            "CON Invalid option\n"
                            "1.Yes\n"
                            "2.No"
                        )

                    numbers = parts[4]

                    valid, cleaned_numbers, error_message = validate_manual_numbers(
                        game,
                        numbers
                    )

                    if not valid:
                        return ussd_response(
                            f"CON Invalid numbers: {error_message}\n"
                            "Please try again."
                        )

                    ref = complete_ticket(
                        phone=phone,
                        game=game,
                        boards=boards,
                        cost=cost,
                        numbers=cleaned_numbers
                    )

                    return ussd_response(
                        f"END Ticket OK\n"
                        f"Ref:{ref}\n"
                        f"{draw_details(game)}"
                    )

        # =========================================
        # POWERBALL
        # =========================================
        if parts[0] == "2":

            if len(parts) == 1:

                return ussd_response(
                    "CON Select\n"
                    "1.PowerBall\n"
                    "2.PowerBall Plus"
                )

            if parts[1] == "1":
                game = "PowerBall"

            elif parts[1] == "2":
                game = "PowerBall Plus"

            else:
                return ussd_response(
                    "END Invalid Option"
                )
            
            if len(parts) == 2:

                return ussd_response(
                    boards_menu()
                )

            if not parts[2].isdigit():

                return ussd_response(
                    "END Invalid Boards"
                )

            boards = int(parts[2])

            if boards < 1 or boards > 5:

                return ussd_response(
                    "END Invalid Boards"
                )

            cost = boards * get_game_price(game)

            ticket_value, service_fee, total_charge = calculate_pricing(
                game,
                boards
            )

            if len(parts) == 3:

                return ussd_response(
                    f"CON R{total_charge} Airtime\n"
                    "1.Quick Pick\n"
                    "2.Manual"
                )

            if parts[3] == "1":

                # Generate PowerBall numbers only once
                if len(parts) == 4:

                    numbers = powerball_quickpick()

                    save_ussd_session(
                        session_id=session_id,
                        phone=phone,
                        game=game,
                        boards=boards,
                        cost=cost,
                        numbers=numbers
                    )

                    return ussd_response(
                        f"CON {game}\n"
                        f"Nums:{numbers}\n"
                        f"R{total_charge} Airtime\n"
                        "1.Confirm\n"
                        "2.Cancel"
                    )

                # Customer confirmation
                if len(parts) == 5:

                    if parts[4] == "2":

                        delete_ussd_session(
                            session_id
                        )

                        return ussd_response(
                            "END Cancelled"
                        )

                    if parts[3] != "1":

                        return ussd_response(
                            "CON Invalid option\n"
                            "1.Confirm\n"
                            "2.Cancel"
                        )

                    pending = get_ussd_session(
                        session_id
                    )

                    if not pending:

                        return ussd_response(
                            "END Session expired. Please try again."
                        )

                    numbers = pending.get(
                        "numbers",
                        ""
                    )

                    ref = complete_ticket(
                        phone=pending.get("phone", phone),
                        game=pending.get("game", game),
                        boards=pending.get("boards", boards),
                        cost=pending.get("cost", cost),
                        numbers=numbers
                    )

                    delete_ussd_session(
                        session_id
                    )

                    return ussd_response(
                        f"END Ticket OK\n"
                        f"Ref:{ref}"
                    )

            if parts[3] == "2":

                # Ask customer for numbers
                if len(parts) == 4:
                    return ussd_response(
                        "CON Enter 5 nums + PB\n"
                        "Example: 1,2,3,4,5,10"
                    )

                # Validate customer-entered numbers
                if len(parts) == 5:
                    numbers = parts[4]

                    valid, cleaned_numbers, error_message = validate_manual_numbers(
                        game,
                        numbers
                    )

                    if not valid:
                        return ussd_response(
                            f"CON Invalid numbers: {error_message}\n"
                            "Please try again."
                        )

                    return ussd_response(
                        f"CON Confirm\n"
                        f"Nums:{cleaned_numbers}\n"
                        f"R{total_charge} Airtime\n"
                        "1.Yes\n"
                        "2.No"
                    )

                # Customer confirmation
                if len(parts) == 6:

                    if parts[5] == "2":
                        delete_ussd_session(session_id)
                        return ussd_response(
                            "END Cancelled"
                        )

                    if parts[5] != "1":
                        return ussd_response(
                            "CON Invalid option\n"
                            "1.Yes\n"
                            "2.No"
                        )

                    numbers = parts[4]

                    valid, cleaned_numbers, error_message = validate_manual_numbers(
                        game,
                        numbers
                    )

                    if not valid:
                        return ussd_response(
                            f"CON Invalid numbers: {error_message}\n"
                            "Please try again."
                        )

                    ref = complete_ticket(
                        phone,
                        game,
                        boards,
                        cost,
                        cleaned_numbers
                    )

                    delete_ussd_session(session_id)

                    return ussd_response(
                        f"END Ticket OK\n"
                        f"Ref:{ref}"
                    )

        # =========================================
        # DAILY LOTTO
        # =========================================
        if parts[0] == "3":

            game = "Daily Lotto"

            if len(parts) == 1:

                return ussd_response(
                    boards_menu()
                )

            if not parts[1].isdigit():

                return ussd_response(
                    "END Invalid Boards"
                )

            boards = int(parts[1])

            if boards < 1 or boards > 5:

                return ussd_response(
                    "END Invalid Boards"
                )

            cost = boards * get_game_price(game)

            ticket_value, service_fee, total_charge = calculate_pricing(
                game,
                boards
            )

            if len(parts) == 2:

                return ussd_response(
                    f"CON R{total_charge} Airtime\n"
                    "1.Quick Pick\n"
                    "2.Manual"
                )

            # QUICK PICK
            if parts[2] == "1":

                # Generate Daily Lotto numbers only once
                if len(parts) == 3:

                    numbers = daily_lotto_quickpick()

                    save_ussd_session(
                        session_id=session_id,
                        phone=phone,
                        game=game,
                        boards=boards,
                        cost=cost,
                        numbers=numbers
                    )

                    return ussd_response(
                        f"CON {game}\n"
                        f"Nums:{numbers}\n"
                        f"R{total_charge} Airtime\n"
                        "1.Confirm\n"
                        "2.Cancel"
                    )

                # Customer confirmation
                if len(parts) == 4:

                    if parts[3] == "2":

                        delete_ussd_session(
                            session_id
                        )

                        return ussd_response(
                            "END Cancelled"
                        )

                    if parts[4] != "1":

                        return ussd_response(
                            "CON Invalid option\n"
                            "1.Confirm\n"
                            "2.Cancel"
                        )

                    pending = get_ussd_session(
                        session_id
                    )

                    if not pending:

                        return ussd_response(
                            "END Session expired. Please try again."
                        )

                    numbers = pending.get(
                        "numbers",
                        ""
                    )

                    ref = complete_ticket(
                        phone=pending.get("phone", phone),
                        game=pending.get("game", game),
                        boards=pending.get("boards", boards),
                        cost=pending.get("cost", cost),
                        numbers=numbers
                    )

                    delete_ussd_session(
                        session_id
                    )

                    return ussd_response(
                        f"END Ticket OK\n"
                        f"Ref:{ref}"
                    )

            # MANUAL
            if parts[2] == "2":

                # Ask customer for numbers
                if len(parts) == 3:

                    return ussd_response(
                        "CON Enter 5 Numbers\n"
                        "1,2,3,4,5"
                    )

                # Validate customer-entered numbers
                if len(parts) == 4:

                    numbers = parts[3]

                    valid, cleaned_numbers, error_message = validate_manual_numbers(
                        game,
                        numbers
                    )

                    if not valid:
                        return ussd_response(
                            f"CON Invalid numbers: {error_message}\n"
                            "Please try again."
                        )

                    return ussd_response(
                        f"CON Confirm\n"
                        f"Nums:{cleaned_numbers}\n"
                        f"R{total_charge} Airtime\n"
                        "1.Yes\n"
                        "2.No"
                    )

                # Customer confirmation
                if len(parts) == 5:

                    if parts[4] == "2":

                        delete_ussd_session(session_id)

                        return ussd_response(
                            "END Cancelled"
                        )

                    if parts[4] != "1":

                        return ussd_response(
                            "CON Invalid option\n"
                            "1.Yes\n"
                            "2.No"
                        )

                    numbers = parts[3]

                    valid, cleaned_numbers, error_message = validate_manual_numbers(
                        game,
                        numbers
                    )

                    if not valid:
                        return ussd_response(
                            f"CON Invalid numbers: {error_message}\n"
                            "Please try again."
                        )

                    ref = complete_ticket(
                        phone=phone,
                        game=game,
                        boards=boards,
                        cost=cost,
                        numbers=cleaned_numbers
                    )

                    delete_ussd_session(session_id)

                    return ussd_response(
                        f"END Ticket OK\n"
                        f"Ref:{ref}"
                    )
                    
        # =========================================
        # RESULTS
        # =========================================
        if parts[0] == "4":

            # -----------------------------------------
            # RESULTS MENU
            # -----------------------------------------
            if len(parts) == 1:

                return ussd_response(
                    "CON Results\n"
                    "1.Lotto\n"
                    "2.Lotto Plus\n"
                    "3.PowerBall\n"
                    "4.PowerBall Plus\n"
                    "5.Daily Lotto"
                )

            try:

                result_game = None

                if parts[1] == "1":
                    result_game = "Lotto"

                elif parts[1] == "2":
                    result_game = "Lotto Plus"

                elif parts[1] == "3":
                    result_game = "PowerBall"

                elif parts[1] == "4":
                    result_game = "PowerBall Plus"

                elif parts[1] == "5":
                    result_game = "Daily Lotto"

                else:

                    return ussd_response(
                        "END Invalid Option"
                    )

                # -----------------------------------------
                # FIND RESULTS FOR SELECTED GAME
                # -----------------------------------------
                result_docs = (
                    db.collection("results")
                    .where(
                        filter=FieldFilter(
                            "game",
                            "==",
                            result_game
                        )
                    )
                    .stream()
                )

                latest_result = None
                latest_date = ""
                latest_date_value = None

                for result_doc in result_docs:

                    result_data = result_doc.to_dict()

                    draw_date = result_data.get(
                        "draw_date",
                        ""
                    ) or ""

                    try:

                        parsed_date = datetime.fromisoformat(
                            str(draw_date).replace("Z", "+00:00")
                        )

                    except (ValueError, TypeError):

                        continue

                    if (
                        latest_date_value is None
                        or parsed_date > latest_date_value
                    ):

                        latest_result = result_data
                        latest_date = draw_date
                        latest_date_value = parsed_date

                # -----------------------------------------
                # NO RESULT FOUND
                # -----------------------------------------
                if latest_result is None:

                    return ussd_response(
                        "END Results Unavailable"
                    )

                # -----------------------------------------
                # DISPLAY RESULT
                # -----------------------------------------
                draw_id = latest_result.get(
                    "draw_id",
                    "N/A"
                )

                numbers = latest_result.get(
                    "official_draw_numbers"
                )

                if not numbers:

                    numbers = latest_result.get(
                        "numbers",
                        []
                    )

                if isinstance(numbers, list):

                    numbers_display = ",".join(
                        str(number)
                        for number in numbers
                    )

                else:

                    numbers_display = str(
                        numbers
                    )

                bonus = latest_result.get(
                    "official_bonus_number"
                )

                powerball = latest_result.get(
                    "powerball"
                )

                response = (
                    f"END {result_game}\n"
                    f"Draw: {draw_id}\n"
                    f"Numbers: {numbers_display}"
                )

                if bonus not in [
                    None,
                    "",
                    "None"
                ]:

                    response += (
                        f"\nBonus: {bonus}"
                    )

                if powerball not in [
                    None,
                    "",
                    "None"
                ]:

                    response += (
                        f"\nPowerBall: {powerball}"
                    )

                print("=" * 60)
                print("USSD RESULTS")
                print("Game:", result_game)
                print("Draw ID:", draw_id)
                print("Draw Date:", latest_date)
                print("Numbers:", numbers_display)
                print("Bonus:", bonus)
                print("PowerBall:", powerball)
                print("=" * 60)

                return ussd_response(
                    response
                )

            except Exception as e:

                print(
                    "Results Error:",
                    str(e)
                )

                return ussd_response(
                    "END Results Unavailable"
                )
            
        # =========================================
        # MY TICKETS
        # =========================================
        if parts[0] == "5":

            try:

                docs = (
                    tickets_ref
                    .where(
                        filter=FieldFilter("phone", "==", phone)
                    )
                    .stream()
                )

                tickets = []

                for doc in docs:

                    ticket = doc.to_dict()

                    # Do not show internal test tickets to customers
                    if ticket.get("is_test") is True:
                        continue

                    tickets.append(
                        (
                            ticket.get("created_at"),
                            f"{ticket.get('game','Unknown')} - "
                            f"{ticket.get('reference', doc.id)}"
                        )
                    )

                # Sort newest first when created_at is available
                tickets.sort(
                    key=lambda item: item[0] or "",
                    reverse=True
                )

                # Keep the USSD response within a reasonable size
                tickets = [
                    item[1]
                    for item in tickets[:5]
                ]

                if tickets:

                    return ussd_response(
                        "END Recent Tickets\n"
                        + "\n".join(tickets)
                    )

                return ussd_response(
                    "END No Tickets Found"
                )

            except Exception as e:

                print(e)

                return ussd_response(
                    "END Unable To Load Tickets"
                )

    except Exception as e:

        print(
            "🔥 USSD ERROR:",
            str(e)
        )

        return ussd_response(
            "END Service Error"
        )
# =========================================
# RUN
# =========================================

if __name__ == "__main__":

    fix_payment_status()

    app.run(
        host="0.0.0.0",
        port=5000
    )

























































