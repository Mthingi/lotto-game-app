import os
import json
import random
from datetime import datetime

import firebase_admin
from firebase_admin import credentials, firestore
from flask import Flask, request

# -------------------------
# Flask App
# -------------------------
app = Flask(__name__)

# -------------------------
# Firebase Initialization
# -------------------------
db = None

try:
    firebase_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    if not firebase_json:
        raise ValueError("FIREBASE_SERVICE_ACCOUNT_JSON is not set")

    cred_dict = json.loads(firebase_json)

    if not firebase_admin._apps:
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)

    db = firestore.client()
    print("✅ Firebase connected")

except Exception as e:
    print(f"❌ Firebase init failed: {e}")

# -------------------------
# Firestore Collections
# -------------------------
lotto_tickets_ref = db.collection("lotto_tickets") if db else None
lotto_results_ref = db.collection("lotto_results") if db else None

# -------------------------
# Prize Structure
# -------------------------
PRIZE_STRUCTURE = {
    6: 1_000_000.00,
    5: 5_000.00,
    4: 500.00,
    3: 50.00
}

# -------------------------
# Helpers
# -------------------------
def generate_winning_numbers():
    return sorted(random.sample(range(1, 60), 6))

def get_latest_lotto_results():
    if not lotto_results_ref:
        return None, None

    results = (
        lotto_results_ref
        .order_by("draw_date", direction=firestore.Query.DESCENDING)
        .limit(1)
        .stream()
    )

    for doc in results:
        data = doc.to_dict()
        return data["winning_numbers"], data["draw_date"]

    # No results → create first draw
    numbers = generate_winning_numbers()
    draw_date = datetime.utcnow()

    lotto_results_ref.add({
        "winning_numbers": numbers,
        "draw_date": draw_date
    })

    return numbers, draw_date

def calculate_winnings(ticket_numbers, winning_numbers):
    matches = len(set(ticket_numbers) & set(winning_numbers))
    return PRIZE_STRUCTURE.get(matches, 0), matches

def valid_numbers(nums):
    return (
        len(nums) == 6 and
        all(n.isdigit() and 1 <= int(n) <= 59 for n in nums)
    )

# -------------------------
# USSD Endpoint
# -------------------------
@app.route("/ussd", methods=["POST"])
def ussd():
    phone = request.form.get("phoneNumber")
    text = request.form.get("text", "")
    parts = text.split("*") if text else []

    # MAIN MENU
    if not parts or parts[0] == "":
        return (
            "CON Welcome to My Phanda Game\n"
            "1. Play Lotto\n"
            "2. View Tickets\n"
            "3. View Results\n"
            "4. Withdraw Winnings"
        )

    # -------------------------
    # PLAY LOTTO
    # -------------------------
    if parts[0] == "1":
        if len(parts) == 1:
            return (
                "CON Enter 6 numbers per board\n"
                "Example:\n"
                "1*2*3*4*5*6\n"
                "Multiple boards:\n"
                "1*2*3*4*5*6,7*8*9*10*11*12"
            )

        if not db:
            return "END System offline. Try again later."

        raw = text.split("*", 1)[1]
        boards = raw.split(",")

        saved = 0

        for board in boards:
            nums = board.split("*")
            if valid_numbers(nums):
                lotto_tickets_ref.add({
                    "phone_number": phone,
                    "numbers": sorted(map(int, nums)),
                    "timestamp": firestore.SERVER_TIMESTAMP,
                    "cashed_out": False
                })
                saved += 1

        return (
            f"END {saved} ticket(s) saved successfully"
            if saved else
            "END Invalid numbers. Use 1–59 only."
        )

    # -------------------------
    # VIEW TICKETS
    # -------------------------
    if parts[0] == "2":
        if not db:
            return "END System offline."

        tickets = (
            lotto_tickets_ref
            .where("phone_number", "==", phone)
            .order_by("timestamp", direction=firestore.Query.DESCENDING)
            .limit(3)
            .stream()
        )

        lines = []
        for t in tickets:
            d = t.to_dict()
            date = d["timestamp"].strftime("%Y-%m-%d") if d.get("timestamp") else "N/A"
            nums = ", ".join(map(str, d["numbers"]))
            lines.append(f"{nums} | {date}")

        return "END No tickets found" if not lines else "END Your tickets:\n" + "\n".join(lines)

    # -------------------------
    # VIEW RESULTS
    # -------------------------
    if parts[0] == "3":
        nums, date = get_latest_lotto_results()
        if not nums:
            return "END Results unavailable"

        return (
            f"END Latest Results ({date.strftime('%Y-%m-%d')}):\n"
            f"{', '.join(map(str, nums))}"
        )

    # -------------------------
    # WITHDRAW
    # -------------------------
    if parts[0] == "4":
        nums, _ = get_latest_lotto_results()
        if not nums:
            return "END Results not ready"

        tickets = (
            lotto_tickets_ref
            .where("phone_number", "==", phone)
            .where("cashed_out", "==", False)
            .stream()
        )

        total = 0

        for t in tickets:
            d = t.to_dict()
            win, _ = calculate_winnings(d["numbers"], nums)
            if win > 0:
                total += win
                t.reference.update({"cashed_out": True})

        return (
            f"END You withdrew R{total:.2f}"
            if total > 0 else
            "END No winnings yet"
        )

    return "END Invalid option"

# -------------------------
# Run App
# -------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
