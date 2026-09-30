import threading


MPG_VAS_FEE_PER_BOARD = 1


def get_game_price(game):
    prices = {
        "Lotto": 6,
        "Lotto Plus": 6,
        "PowerBall": 5,
        "PowerBall Plus": 5,
        "Daily Lotto": 3
    }

    return prices.get(game, 0)


def calculate_pricing(game, boards):
    ticket_value = boards * get_game_price(game)
    service_fee = boards * MPG_VAS_FEE_PER_BOARD
    total_charged = ticket_value + service_fee

    return ticket_value, service_fee, total_charged


tickets_ref = None
send_sms = None


def init_worker(firebase_ref, sms_sender):
    global tickets_ref, send_sms

    tickets_ref = firebase_ref
    send_sms = sms_sender


def process_ticket(
    phone,
    game,
    boards,
    cost,
    ref,
    numbers
):

    def job():

        try:

            if send_sms:

                print("SMS sender:", send_sms)

                print(
                    "Sending SMS to",
                    phone
                )

                ticket_value, service_fee, total_charged = calculate_pricing(
                    game,
                    boards
                )

                message = (
                    f"MPG Ticket\n"
                    f"{game}\n"
                    f"Ref:{ref}\n"
                    f"Ticket Value:R{ticket_value}\n"
                    f"MPG Fee:R{service_fee}\n"
                    f"Total Airtime:R{total_charged}\n"
                    f"Numbers:{numbers}"
                )

                response = send_sms(
                    phone,
                    message
                )

                print("SMS Response:")
                print(response)

            else:

                print("SMS sender not initialized")

            print(
                "Worker completed:",
                ref
            )

        except Exception as e:

            print("Worker error:", e)

    thread = threading.Thread(
        target=job,
        daemon=True
    )

    thread.start()