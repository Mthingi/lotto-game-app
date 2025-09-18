import os
import firebase_admin
from firebase_admin import credentials, firestore
from flask import Flask, request
from datetime import datetime, timedelta
import random

# Initialize Flask app
app = Flask(__name__)

# Try to initialize Firebase Admin SDK from the environment variable.
# If the environment variable is not set, the app will not run.
try:
    # Get the service account key from the environment variable
    firebase_service_account_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")

    if not firebase_service_account_json:
        raise ValueError("FIREBASE_SERVICE_ACCOUNT_JSON environment variable is not set.")

    # Create the credentials file from the environment variable data
    credentials_file_path = "firebase-admin-sdk-credentials.json"
    with open(credentials_file_path, "w") as f:
        f.write(firebase_service_account_json)

    # Initialize Firebase app
    cred = credentials.Certificate(credentials_file_path)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
        print("✅ Firebase connected successfully.")

    db = firestore.client()

except ValueError as e:
    print(f"❌ Firebase initialization failed: {e}")
    db = None
except FileNotFoundError as e:
    print(f"❌ File not found: {e}. Please ensure you have read/write permissions in the current directory.")
    db = None
except Exception as e:
    print(f"❌ An unexpected error occurred during Firebase initialization: {e}")
    db = None

# Firestore references
lotto_ref = db.collection('lotto') if db else None
lotto_results_ref = db.collection('lotto_results') if db else None

# Define a simple prize structure based on number of matches
PRIZE_STRUCTURE = {
    6: 1000000.00,  # Jackpot
    5: 5000.00,
    4: 500.00,
    3: 50.00
}

def lotto_draw_is_open():
    """
    Returns True to indicate that the lotto draw is always open.
    This bypasses the previous time-checking logic, allowing users to play at any time.
    """
    return True

def generate_winning_numbers():
    """Generates a random set of 6 winning lotto numbers."""
    return sorted(random.sample(range(1, 60), 6))

def get_latest_lotto_results():
    """
    Fetches the latest lotto draw results from Firestore. If none exist,
    it generates and saves a new set of winning numbers.
    """
    if not lotto_results_ref:
        return None, "Database is not connected."

    try:
        # Get the most recent draw result
        results_query = lotto_results_ref.order_by('draw_date', direction=firestore.Query.DESCENDING).limit(1)
        results = list(results_query.stream())

        if results:
            latest_result = results[0].to_dict()
            return latest_result['winning_numbers'], latest_result['draw_date']
        else:
            # If no results exist, generate new ones for the first time
            winning_numbers = generate_winning_numbers()
            draw_date = datetime.now()
            lotto_results_ref.add({
                'winning_numbers': winning_numbers,
                'draw_date': draw_date
            })
            print("✅ Generated and saved new lotto draw results.")
            return winning_numbers, draw_date

    except Exception as e:
        print(f"❌ Error fetching or creating lotto results: {e}")
        return None, "An error occurred fetching results."

def calculate_winnings(ticket_numbers, winning_numbers):
    """Calculates winnings based on the number of matches."""
    # Convert lists to sets for efficient intersection calculation
    ticket_set = set(ticket_numbers)
    winning_set = set(winning_numbers)

    matches = len(ticket_set.intersection(winning_set))
    return PRIZE_STRUCTURE.get(matches, 0.00), matches

@app.route("/ussd", methods=['POST'])
def ussd_callback():
    session_id = request.form.get("sessionId")
    service_code = request.form.get("serviceCode")
    phone_number = request.form.get("phoneNumber")
    text = request.form.get("text", "default").split('*')
    raw_text = request.form.get("text", "default")

    response = ""

    # Check if the lotto draw is open before proceeding
    if not lotto_draw_is_open():
        response = "END The Lotto draw is currently closed. Please try again on a later day."
        return response

    if text[0] == "default":
        # Initial menu
        response = "CON Welcome to the Lotto game app\n"
        response += "1. Play Lotto\n"
        response += "2. View my ticket\n"
        response += "3. View results\n"
        response += "4. Withdraw winnings"
    
    elif text[0] == "1":
        # Menu for playing lotto
        if len(text) == 1:
            response = "CON You are about to play the lotto.\n"
            response += "Enter your 6 lucky numbers (e.g., 10*20*30*40*50*60). You can play multiple boards by separating them with a comma (e.g., 1*2*3*4*5*6,7*8*9*10*11*12)"
        else:
            # Process multiple boards
            if not db:
                response = "END Thank you for playing the lotto. Database is not connected, your numbers were not saved."
                return response

            # Get the full string of numbers after the initial "1" command
            try:
                raw_boards_string = raw_text.split('*', 1)[1]
            except IndexError:
                response = "END Invalid entry. Please enter your lucky numbers."
                return response
            
            # Split the string by comma to get individual boards
            boards_list = raw_boards_string.split(',')
            
            saved_tickets_count = 0
            
            for board_str in boards_list:
                lucky_numbers = board_str.split('*')
                
                # Validate the board
                if len(lucky_numbers) == 6 and all(n.isdigit() for n in lucky_numbers):
                    try:
                        # Save the valid board to Firestore
                        doc_ref = db.collection('lotto_tickets').add({
                            'phone_number': phone_number,
                            'numbers': sorted([int(n) for n in lucky_numbers]),
                            'timestamp': firestore.SERVER_TIMESTAMP,
                            'cashed_out': False
                        })
                        saved_tickets_count += 1
                    except Exception as e:
                        print(f"❌ Error writing to Firestore: {e}")
                        # Continue to the next board even if one fails
                        
            if saved_tickets_count > 0:
                response = f"END Thank you for playing the lotto. {saved_tickets_count} ticket(s) have been saved successfully."
            else:
                response = "END Invalid entry. Please ensure each board has exactly 6 numbers (1-59) separated by asterisks, and multiple boards are separated by a comma. (e.g., 1*2*3*4*5*6,7*8*9*10*11*12)"
    
    elif text[0] == "2":
        # View my ticket
        if db:
            try:
                tickets_ref = db.collection('lotto_tickets').where('phone_number', '==', phone_number).order_by('timestamp', direction=firestore.Query.DESCENDING).limit(3)
                tickets = list(tickets_ref.stream())
                
                if tickets:
                    response = "CON Your recent tickets:\n"
                    for i, ticket in enumerate(tickets):
                        ticket_data = ticket.to_dict()
                        numbers = ', '.join([str(n) for n in ticket_data['numbers']])
                        # Firestore timestamp object needs to be converted to a datetime object
                        timestamp = ticket_data['timestamp'].strftime("%Y-%m-%d %H:%M")
                        response += f"{i+1}. Numbers: {numbers} | Date: {timestamp}\n"
                    response += "0. Back to Main Menu"
                else:
                    response = "END You have no tickets saved."
            except Exception as e:
                print(f"❌ Error fetching tickets from Firestore: {e}")
                response = "END An error occurred while fetching your tickets. Please try again."
        else:
            response = "END We cannot retrieve your tickets. Database is not connected."
    
    elif text[0] == "3":
        # View results
        winning_numbers, draw_date = get_latest_lotto_results()
        if winning_numbers:
            numbers_str = ', '.join([str(n) for n in winning_numbers])
            date_str = draw_date.strftime("%Y-%m-%d %H:%M")
            response = f"END Latest lotto results ({date_str}):\nWinning numbers: {numbers_str}"
        else:
            response = "END Could not retrieve lotto results. Please try again later."

    elif text[0] == "4":
        # Withdraw winnings
        if not db:
            response = "END We cannot process your request. Database is not connected."
            return response
        
        try:
            winning_numbers, draw_date = get_latest_lotto_results()
            if not winning_numbers:
                response = "END We're still waiting on the latest lotto results. Please try again later."
                return response
            
            # Get tickets for the user that have not been cashed out
            tickets_ref = db.collection('lotto_tickets').where('phone_number', '==', phone_number).where('cashed_out', '==', False).stream()
            tickets_to_cash_out = list(tickets_ref)
            
            total_winnings = 0.00
            winnings_breakdown = []
            
            for ticket in tickets_to_cash_out:
                ticket_data = ticket.to_dict()
                winnings, matches = calculate_winnings(ticket_data['numbers'], winning_numbers)
                
                if winnings > 0:
                    total_winnings += winnings
                    winnings_breakdown.append(f"Ticket from {ticket_data['timestamp'].strftime('%Y-%m-%d')}: {matches} matches, won R{winnings:.2f}")
                    
                    # Mark the ticket as cashed out
                    ticket.reference.update({'cashed_out': True, 'cash_out_timestamp': firestore.SERVER_TIMESTAMP})

            if total_winnings > 0:
                response = f"END Congratulations! You have successfully withdrawn your winnings.\nTotal amount: R{total_winnings:.2f}\n\nBreakdown:\n"
                response += "\n".join(winnings_breakdown)
            else:
                response = "END You have no recent winnings to withdraw. Keep playing!"
        
        except Exception as e:
            print(f"❌ Error processing withdrawal: {e}")
            response = "END An error occurred while processing your withdrawal. Please try again later."

    else:
        response = "END Invalid input. Please try again."

    return response

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8000, debug=True)
