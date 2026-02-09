from flask import Flask, request, Response
import firebase_admin
from firebase_admin import credentials, firestore
import africastalking
import os
import uuid
import random
from datetime import datetime, timedelta

# ==============================
# ENV VARIABLES
# ==============================
AT_USERNAME = os.getenv("AT_USERNAME")
AT_API_KEY = os.getenv("AT_API_KEY")

if not AT_USERNAME or not AT_API_KEY:
    raise Exception("Africa's Talking credentials not set")

# ==============================
# FLASK
# ==============================
app = Flask(__name__)

# ==============================
# FIREBASE
# ==============================
cred = credentials.Certificate("firebase.json")
firebase_admin.initialize_app(cred)
db = firestore.client()
tickets_ref = db.collection("tickets")

# ==============================
# AFRICA'S TALKING (NEW SDK STYLE)
# ==============================
# Initialize Africa's Talking
africastalking.initialize(username=AT_USERNAME, api_key=AT_API_KEY)

# Create service instances
airtime = africastalking.Airtime
sms = africastalking.SMS

# ==============================
# HELPERS
# ==============================
def generate_receipt():
    return str(uuid.uuid4())[:8].upper()

def game_name(code):
    return {
        "1": "Lotto",
        "2": "Lotto Plus",
        "3": "Lotto Plus 2",
        "4": "PowerBall",
        "5": "PowerBall Plus",
        "6": "Daily Lotto"
    }.get(code, "Unknown")

def next_draw_date(game):
    today = datetime.now()
    draw_days = {
        "1": [2, 5],  # Wed, Sat
        "2": [2, 5],
        "3": [2, 5],
        "4": [1, 4],  # Tue, Fri
        "5": [1, 4],
        "6": list(range(7))  # Daily
    }
    for i in range(1, 8):
        d = today + timedelta(days=i)
        if d.weekday() in draw_days.get(game, []):
            return d.strftime("%Y-%m-%d")
    return today.strftime("%Y-%m-%d")

def generate_board():
    """Auto-pick 6 random numbers"""
    return sorted(random.sample(range(1, 50), 6))

def calculate_total_cost(num_boards):
    """Ithuba-style pricing: R5 per board"""
    return 5 * num_boards

def deduct_airtime(phone, amount):
    """Deduct airtime (amount in ZAR)"""
    try:
        response = airtime.send(phone, amount)
        return True, response
    except Exception as e:
        print("Airtime deduction failed:", e)
        return False, str(e)

def send_sms_confirmation(phone, message):
    """SMS function stub - disabled for now"""
    # sms.send(message, [phone])
    pass

# ==============================
# USSD ENDPOINT
# ==============================
@app.route("/ussd", methods=["POST"])
def ussd():
    phone = request.form.get("phoneNumber")
    text = request.form.get("text", "")
    parts = text.split("*")

    # Step 1: Welcome
    if text == "":
        return Response(
            "CON Welcome to My Phanda Game\n"
            "1. Play\n"
            "2. My Tickets",
            mimetype="text/plain"
        )

    # Step 2: Play menu
    if parts[0] == "1" and len(parts) == 1:
        return Response(
            "CON Choose Game:\n"
            "1. Lotto\n"
            "2. Lotto Plus\n"
            "3. Lotto Plus 2\n"
            "4. PowerBall\n"
            "5. PowerBall Plus\n"
            "6. Daily Lotto",
            mimetype="text/plain"
        )

    # Step 3: Enter number of boards
    if parts[0] == "1" and len(parts) == 2:
        return Response(
            "CON Enter number of boards (1-7):",
            mimetype="text/plain"
        )

    # Step 4: Enter numbers or Auto-pick
    if parts[0] == "1" and len(parts) == 3:
        game = parts[1]
        try:
            num_boards = int(parts[2])
            if not 1 <= num_boards <= 7:
                raise ValueError
        except:
            return Response(
                "CON Invalid number of boards. Enter 1-7:",
                mimetype="text/plain"
            )

        # Ask for numbers for each board or allow auto-pick
        msg = "CON Enter numbers for each board (comma separated) or type 'A' for auto-pick\n"
        for i in range(1, num_boards + 1):
            msg += f"Board {i}: \n"
        return Response(msg, mimetype="text/plain")

    # Step 5: Save tickets
    if parts[0] == "1" and len(parts) >= 4:
        game = parts[1]
        num_boards = int(parts[2])
        board_inputs = parts[3:]

        boards = []
        for board in board_inputs:
            if board.upper() == "A":
                boards.append(generate_board())
            else:
                try:
                    nums = list(map(int, board.split(",")))
                    if len(nums) != 6 or any(not 1 <= n <= 49 for n in nums):
                        raise ValueError
                    boards.append(nums)
                except:
                    return Response(
                        f"CON Invalid numbers: {board}\nEnter 6 numbers 1-49 or 'A' for auto-pick",
                        mimetype="text/plain"
                    )

        total_cost = calculate_total_cost(len(boards))
        success, response = deduct_airtime(phone, total_cost)
        if not success:
            return Response(f"END Airtime deduction failed: {response}", mimetype="text/plain")

        receipts = []
        draw_date = next_draw_date(game)

        for board in boards:
            receipt = generate_receipt()
            tickets_ref.add({
                "phone": phone,
                "game": game_name(game),
                "numbers": ",".join(map(str, board)),
                "receipt": receipt,
                "draw_date": draw_date,
                "created_at": firestore.SERVER_TIMESTAMP
            })
            receipts.append(receipt)
            # send_sms_confirmation(phone, f"Ticket {receipt} confirmed!")  # disabled for now

        return Response(
            f"END Ticket(s) Played!\n"
            f"Game: {game_name(game)}\n"
            f"Boards: {len(boards)}\n"
            f"Draw: {draw_date}\n"
            f"Receipts:\n" + "\n".join(receipts),
            mimetype="text/plain"
        )

    # Step: View tickets
    if parts[0] == "2":
        docs = tickets_ref.where("phone", "==", phone).stream()
        receipts = [d.to_dict()["receipt"] for d in docs]

        if not receipts:
            return Response("END No tickets found", mimetype="text/plain")

        return Response(
            "END Your tickets:\n" + "\n".join(receipts),
            mimetype="text/plain"
        )

    return Response("END Invalid option", mimetype="text/plain")

# ==============================
# RUN
# ==============================
if __name__ == "__main__":
    app.run(debug=True)
