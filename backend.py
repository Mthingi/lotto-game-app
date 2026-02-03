@app.route("/ussd", methods=["POST"])
def ussd():
    phone = request.form.get("phoneNumber", "")
    raw_text = request.form.get("text", "").strip()

    # Normalize Africa's Talking quirks
    raw_text = raw_text.replace("##", "").rstrip("*")
    parts = raw_text.split("*") if raw_text else []

    # =============================
    # MAIN MENU
    # =============================
    if len(parts) == 0:
        return Response(
            "CON My Phanda Game\n"
            "1. Play Lotto\n"
            "2. My Receipts\n"
            "3. Results",
            mimetype="text/plain"
        )

    # =============================
    # PLAY LOTTO
    # =============================
    if parts[0] == "1":

        # Step 1: Select game
        if len(parts) == 1:
            return Response(
                "CON Select Game\n"
                "1. Lotto\n"
                "2. Lotto Plus\n"
                "3. Lotto Plus 2\n"
                "4. PowerBall\n"
                "5. PowerBall Plus\n"
                "6. Daily Lotto",
                mimetype="text/plain"
            )

        game_map = {
            "1": "LOTTO",
            "2": "LOTTO_PLUS",
            "3": "LOTTO_PLUS_2",
            "4": "POWERBALL",
            "5": "POWERBALL_PLUS",
            "6": "DAILY_LOTTO"
        }

        game = game_map.get(parts[1])
        if not game:
            return Response("END Invalid game selection", mimetype="text/plain")

        # Step 2: Ask for boards
        if len(parts) == 2:
            return Response(
                "CON Enter number of boards (1-5)",
                mimetype="text/plain"
            )

        # Step 3: Validate boards
        try:
            boards_count = int(parts[2])
            if boards_count < 1 or boards_count > 5:
                raise ValueError
        except ValueError:
            return Response(
                "CON Enter number of boards (1-5)",
                mimetype="text/plain"
            )

        # =============================
        # PROCESS TICKET
        # =============================
        boards = [generate_numbers(game) for _ in range(boards_count)]
        receipt_id = generate_receipt()
        draw_date = next_draw_date(game)

        # Save ticket (non-blocking)
        if tickets_ref:
            try:
                tickets_ref.add({
                    "receipt_id": receipt_id,
                    "phone": phone,
                    "game": game,
                    "boards": boards,
                    "draw_date": draw_date,
                    "status": "PLAYED",
                    "created_at": firestore.SERVER_TIMESTAMP
                })
            except Exception as e:
                print("Firebase error:", e)

        # Send SMS (safe)
        if sms:
            try:
                sms.send(
                    f"My Phanda Game\n"
                    f"Receipt: {receipt_id}\n"
                    f"Game: {game.replace('_', ' ')}\n"
                    f"Boards: {boards_count}\n"
                    f"Draw: {draw_date.strftime('%Y-%m-%d')}",
                    [phone]
                )
            except Exception as e:
                print("SMS error:", e)

        return Response(
            "END Play successful\n"
            f"Receipt: {receipt_id}\n"
            f"Draw: {draw_date.strftime('%Y-%m-%d')}",
            mimetype="text/plain"
        )

    return Response("END Invalid option", mimetype="text/plain")
