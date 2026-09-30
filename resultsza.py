import os
import requests
from dotenv import load_dotenv


# =========================================================
# RESULTSZA CONFIGURATION
# =========================================================

load_dotenv()

API_KEY = os.getenv("RESULTSZA_API_KEY")

if not API_KEY:
    raise RuntimeError(
        "RESULTSZA_API_KEY was not found in .env"
    )

BASE_URL = "https://resultsza.co.za/api"


# =========================================================
# RESULTSZA GAME ENDPOINTS
# =========================================================

GAMES = {
    "Lotto": "get_lotto_results",
    "Lotto Plus": "get_lotto_plus_1_results",
    "PowerBall": "get_sa_powerball_results",
    "PowerBall Plus": "get_powerball_xtra_results",
    "Daily Lotto": "get_daily_lotto_results",
}


# =========================================================
# GET RESULT FOR ONE GAME
# =========================================================

def get_result(game_name):

    if game_name not in GAMES:
        raise ValueError(
            f"Unsupported game: {game_name}"
        )

    endpoint = GAMES[game_name]

    url = f"{BASE_URL}/{endpoint}"

    params = {
        "api_key": API_KEY
    }

    response = requests.get(
        url,
        params=params,
        timeout=20
    )

    response.raise_for_status()

    data = response.json()

    if data.get("status") != "success":
        raise RuntimeError(
            f"ResultsZA returned an error: {data}"
        )

    results = data.get("results", [])

    if not results:
        raise RuntimeError(
            f"No result returned for {game_name}"
        )

    return results[0]


# =========================================================
# NORMALIZE RESULTS FOR MPG
# =========================================================

def get_mpg_result(game_name):

    result = get_result(game_name)

    normalized = {
        "game": game_name,
        "draw_id": result.get("draw_id"),
        "draw_date": result.get("draw_date"),
        "winning_numbers": result.get("winning_numbers", []),
        "bonus_ball": result.get("bonus_ball"),
        "divisions": result.get("divisions", []),
    }

    # PowerBall games use "powerball" instead of "bonus_ball"
    if game_name in [
        "PowerBall",
        "PowerBall Plus"
    ]:
        normalized["powerball"] = result.get("powerball")

    return normalized

# =========================================================
# PREPARE RESULT FOR FIRESTORE
# =========================================================

def get_firestore_result(game_name):

    result = get_mpg_result(game_name)

    firestore_result = {
        "game": result["game"],
        "draw_id": result["draw_id"],
        "draw_date": result["draw_date"],
        "draw_numbers": result["winning_numbers"],
        "bonus_ball": result.get("bonus_ball"),
        "divisions": result.get("divisions", []),
        "source": "ResultsZA",
        "result_checked": False,
        "processed_at": None,
        "matched_numbers": [],
    }

    # PowerBall games use a separate PowerBall number
    if game_name in [
        "PowerBall",
        "PowerBall Plus"
    ]:
        firestore_result["powerball"] = result.get(
            "powerball"
        )

    return firestore_result


# =========================================================
# TEST ALL SOUTH AFRICAN GAMES
# =========================================================

def test_all_games():

    print("=" * 70)
    print("RESULTSZA — SOUTH AFRICAN GAME TEST")
    print("=" * 70)
    print()

    for game_name in GAMES:

        print("-" * 70)
        print(f"Testing: {game_name}")
        print("-" * 70)

        try:

            result = get_result(game_name)

            print("Status: SUCCESS")
            print("Game:", result.get("game_type"))
            print("Draw ID:", result.get("draw_id"))
            print("Draw Date:", result.get("draw_date"))
            print("Winning Numbers:", result.get("winning_numbers"))

            if game_name in [
                "PowerBall",
                "PowerBall Xtra"
            ]:
                print(
                    "PowerBall:",
                    result.get("powerball")
                )
            else:
                print(
                    "Bonus Ball:",
                    result.get("bonus_ball")
                )

            print()

        except Exception as e:

            print("Status: FAILED")
            print("Error:", e)
            print()

    print("=" * 70)
    print("RESULTSZA GAME TEST COMPLETE")
    print("=" * 70)


# =========================================================
# RUN TEST
# =========================================================

# =========================================================
# RUN FIRESTORE STRUCTURE TEST
# =========================================================

if __name__ == "__main__":

    result = get_firestore_result("Lotto")

    print()
    print("=" * 70)
    print("MPG FIRESTORE-READY RESULT")
    print("=" * 70)

    for key, value in result.items():
        print(f"{key}: {value}")

    print("=" * 70)