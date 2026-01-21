import os
import json
import random
from datetime import datetime

import firebase_admin
from firebase_admin import credentials, firestore
from flask import Flask, request

# =========================
# DRAW DAYS (SA LOTTO RULES)
# =========================
DRAW_DAYS = {
    "LOTTO": ["Wednesday", "Saturday"],
    "LOTTO_PLUS": ["Wednesday", "Saturday"],
    "LOTTO_PLUS_2": ["Wednesday", "Saturday"],
    "POWERBALL": ["Tuesday", "Friday"],
    "POWERBALL_PLUS": ["Tuesday", "Friday"],
    "DAILY_LOTTO": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
}

def today_name():
    return datetime.now().strftime("%A")

def is_game_available(game_key):
    return today_name() in DRAW_DAYS.get(game_key, [])

# =========================
# FLASK APP
# =========================
app = Flask(__name__)

# =========================
# FIREBASE INIT (RENDER SAFE)
# =========================
db = None

try:
    firebase_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    if not firebase_json:
        raise ValueError("FIREBASE_SERVICE_ACCOUNT_JSON not set")

    cred_dict = json.loads(firebase_json)

    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(cred_dict))

    db = firestore.client()
    print("✅ Firebase connected")

except Exception as e:
    print(f"❌ Firebase init failed: {e}")

# =========================
# COLLECTIONS
# =========================
tickets_ref = db.collection("lotto_tickets") if db else None
results_ref = db.collection("lotto_results") if db else None

# =========================
# PRIZES (DEMO VALUES)
# =========================
PRIZES = {
    6: 1_000_000,
    5: 5_000,
    4: 500,
    3: 50
}

# =========================
# HELPERS
# =========================
def generate_numbers():
    return sorted(random.sample(range(1, 60), 6))

def valid_numbers(nums):
    return len(nums) == 6 and all(n.isdigit() and 1 <= int(n) <= 59 for n in nums)

def calculate_winnings(ticket, winning):
    matches = len(set(ticket) & set(winning))
    return PRIZES.get(matches, 0), matches

def get_latest_results(game):
    if not results_ref:
        return None, None

    query = (
        results_ref
        .where("game", "==", game)
        .order_by("draw_date", direction=firestore.Query.DESCENDING)
        .limit(1)
        .stream()
    )

    for doc in query:
        data = doc.to_dict()
        return data["numbers"], data["draw_date"]

    numbers = generate_numbers()
    draw_date = datetime.utcnow()

    results_ref.add({
        "game": game,
        "numbers": numbers,
        "draw_date": draw_date
    })

    return numbers, draw_date

# =========================
# USSD ENDPOINT
# =========================
@app.route("/ussd", methods=["POST"])
def ussd():
    phone = request.form.get("phoneNumber")
    text = request.form.get("text", "")
    parts = text.split("*") if text else []

    # MAIN MENU
    if not parts or parts[0] == "":
        return (
            "CON My Phanda Game\n"
            "1. Play Lotto Games\n"
            "2. My Tickets\n"
            "3. View Results\n"
            "4. Withdraw Winnings"
        )

    # =====================
    # PLAY MENU
    # =====================
    if parts[0] == "1":
        if len(parts) == 1:
            return (
                "CON Choose Game\n"
                "1. Lotto\n"
                "2. Lotto Plus\n"
                "3. Lotto Plus 2\n"
                "4. PowerBall\n"
                "5. PowerBall Plus\n"
                "6. Daily Lotto"
            )

        game_map = {
            "1": "LOTTO",
            "2": "LOTTO_PLUS",
            "3": "LOTTO_PLUS_2",
            "4": "POWERBALL",
            "5": "POWERBALL_PLUS",
            "6": "DAILY_LOTTO",
        }

        game = game_map.get(parts[1])
        if not game:
            return "END Invalid game"

        if not is_game_available(game):
            return f"END {game.replace('_', ' ')} not available today"

        if len(parts) == 2:
            return "CON Enter 6 numbers\nExample: 1*2*3*4*5*6"

        if not db:
            return "END System offline"

        nums = parts[2:]
        if not valid_numbers(nums):
            return "END Invalid numbers (1–59)"

        tickets_ref.add({
            "phone": phone,
            "game": game,
            "numbers": sorted(map(int, nums)),
            "timestamp": firestore.SERVER_TIMESTAMP,
            "cashed_out": False
        })

        return f"END {game.replace('_',' ')} ticket saved"

    # =====================
    # MY TICKETS
    # =====================
    if parts[0] == "2":
        if not db:
            return "END System offline"

        query = (
            tickets_ref
            .where("phone", "==", phone)
            .order_by("timestamp", direction=firestore.Query.DESCENDING)
            .limit(5)
            .stream()
        )

        lines = []
        for t in query:
            d = t.to_dict()
            nums = ", ".join(map(str, d["numbers"]))
            lines.append(f"{d['game']}: {nums}")

        return "END No tickets found" if not lines else "END Your Tickets:\n" + "\n".join(lines)

    # =====================
    # RESULTS
    # =====================
    if parts[0] == "3":
        return (
            "CON Results\n"
            "1. Lotto\n"
            "2. PowerBall\n"
            "3. Daily Lotto"
        )

    # =====================
    # WITHDRAW
    # =====================
    if parts[0] == "4":
        if not db:
            return "END System offline"

        total = 0

        tickets = (
            tickets_ref
            .where("phone", "==", phone)
            .where("cashed_out", "==", False)
            .stream()
        )

        for t in tickets:
            d = t.to_dict()
            winning, _ = get_latest_results(d["game"])
            if not winning:
                continue

            win, _ = calculate_winnings(d["numbers"], winning)
            if win > 0:
                total += win
                t.reference.update({"cashed_out": True})

        return f"END Withdrawn R{total:.2f}" if total > 0 else "END No winnings yet"

    return "END Invalid option"

# =========================
# RUN
# =========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
