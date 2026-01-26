import os
import json
import random
import uuid
from datetime import datetime, timedelta

from flask import Flask, request
import firebase_admin
from firebase_admin import credentials, firestore

# =============================
# Flask App
# =============================
app = Flask(__name__)

# =============================
# Draw Days (SA Rules)
# =============================
DRAW_DAYS = {
    "LOTTO": [2, 5],          # Wed, Sat
    "LOTTO_PLUS": [2, 5],
    "LOTTO_PLUS_2": [2, 5],
    "POWERBALL": [1, 4],      # Tue, Fri
    "POWERBALL_PLUS": [1, 4],
    "DAILY_LOTTO": [0, 1, 2, 3, 4, 5, 6]
}

# =============================
# Firebase Init (ENV SAFE)
# =============================
db = None
try:
    firebase_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    if firebase_json:
        cred = credentials.Certificate(json.loads(firebase_json))
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("✅ Firebase connected")
    else:
        print("⚠ Firebase env not set (local mode)")
except Exception as e:
    print(f"❌ Firebase init failed: {e}")

tickets_ref = db.collection("tickets") if db else None
results_ref = db.collection("results") if db else None

# =============================
# Helpers
# =============================
def next_draw_date(game):
    today = datetime.utcnow()
    for i in range(1, 8):
        d = today + timedelta(days=i)
        if d.weekday() in DRAW_DAYS[game]:
            return d
    return today

def generate_numbers(game):
    if "POWERBALL" in game:
        return {
            "main": sorted(random.sample(range(1, 51), 5)),
            "powerball": random.randint(1, 20)
        }
    return sorted(random.sample(range(1, 60), 6))

def generate_receipt():
    return f"MPG-{uuid.uuid4().hex[:10].upper()}"

# =============================
# WIN CHECK HELPERS (STEP 2)
# =============================
PRIZES = {
    6: 1_000_000,
    5: 5_000,
    4: 500,
    3: 50
}

def calculate_matches(board, winning_numbers):
    return len(set(board) & set(winning_numbers))

def calculate_board_win(board, winning_numbers):
    matches = calculate_matches(board, winning_numbers)
    return PRIZES.get(matches, 0), matches

def get_latest_result(game):
    if not results_ref:
        return None

    docs = (
        results_ref
        .where("game", "==", game)
        .order_by("draw_date", direction=firestore.Query.DESCENDING)
        .limit(1)
        .stream()
    )

    for d in docs:
        return d.to_dict()

    return None

def process_draw_results(game):
    result = get_latest_result(game)
    if not result:
        return

    winning_numbers = result["numbers"]
    draw_date = result["draw_date"]

    receipts = (
        tickets_ref
        .where("game", "==", game)
        .where("status", "==", "PLAYED")
        .stream()
    )

    for receipt in receipts:
        data = receipt.to_dict()

        if data["draw_date"] > draw_date:
            continue

        total_win = 0
        breakdown = []

        for board in data["boards"]:
            win, matches = calculate_board_win(board, winning_numbers)
            breakdown.append({
                "numbers": board,
                "matches": matches,
                "win": win
            })
            total_win += win

        receipt.reference.update({
            "status": "WON" if total_win > 0 else "LOST",
            "total_winnings": total_win,
            "breakdown": breakdown,
            "checked_at": firestore.SERVER_TIMESTAMP
        })

# =============================
# USSD Endpoint
# =============================
@app.route("/ussd", methods=["POST"])
def ussd():
    phone = request.form.get("phoneNumber")
    text = request.form.get("text", "")
    parts = text.split("*") if text else []

    # MAIN MENU
    if not parts or parts[0] == "":
        return (
            "CON My Phanda Game\n"
            "1. Play Lotto\n"
            "2. My Receipts\n"
            "3. Results"
        )

    # =============================
    # PLAY LOTTO
    # =============================
    if parts[0] == "1":
        if len(parts) == 1:
            return (
                "CON Select Game\n"
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

        if len(parts) == 2:
            return "CON How many boards? (1-5)"

        try:
            boards_count = int(parts[2])
        except:
            return "END Invalid input"

        if boards_count < 1 or boards_count > 5:
            return "END Max 5 boards allowed"

        boards = [generate_numbers(game) for _ in range(boards_count)]
        receipt_id = generate_receipt()
        draw_date = next_draw_date(game)

        ticket = {
            "receipt_id": receipt_id,
            "phone": phone,
            "game": game,
            "boards": boards,
            "draw_date": draw_date,
            "status": "PLAYED",
            "created_at": firestore.SERVER_TIMESTAMP,
            "sms_confirmation": (
                f"My Phanda Game\n"
                f"Receipt: {receipt_id}\n"
                f"Game: {game.replace('_',' ')}\n"
                f"Boards: {boards_count}\n"
                f"Draw: {draw_date.strftime('%Y-%m-%d')}"
            )
        }

        if db:
            tickets_ref.add(ticket)

        return (
            "END Play confirmed!\n"
            f"Receipt: {receipt_id}\n"
            f"Draw: {draw_date.strftime('%Y-%m-%d')}\n"
            "SMS confirmation sent"
        )

    # =============================
    # VIEW RECEIPTS
    # =============================
    if parts[0] == "2":
        if not db:
            return "END Service unavailable"

        docs = (
            tickets_ref
            .where("phone", "==", phone)
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(3)
            .stream()
        )

        lines = []
        for d in docs:
            t = d.to_dict()
            lines.append(f"{t['game']} | {t['receipt_id']} | {t['status']}")

        return "END No plays found" if not lines else "END Receipts:\n" + "\n".join(lines)

    # =============================
    # RESULTS
    # =============================
    if parts[0] == "3":
        return "END Results checked automatically after official draw"

    return "END Invalid option"

# =============================
# Run App
# =============================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
