import json
from datetime import datetime, timedelta

RECENT_WINDOW_DAYS = 30
MIN_RECENT_MATCHES = 2


def _parse_date(date_str):
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%d-%m-%y")
    except ValueError:
        return None


def _analyze_event(event):
    h2h = event.get("h2h") or []
    if not h2h:
        return None

    home_wins = 0
    away_wins = 0
    recent_count = 0
    cutoff = datetime.utcnow() - timedelta(days=RECENT_WINDOW_DAYS)

    for match in h2h:
        winner = match.get("winner")
        if winner == "home":
            home_wins += 1
        elif winner == "away":
            away_wins += 1

        d = _parse_date(match.get("date"))
        if d and d >= cutoff:
            recent_count += 1

    total = len(h2h)

    won_all = False
    won_4_of_5 = False
    dominant_side = None

    if total > 0 and home_wins == total:
        won_all = True
        dominant_side = "home"
    elif total > 0 and away_wins == total:
        won_all = True
        dominant_side = "away"
    elif total == 5:
        if home_wins == 4:
            won_4_of_5 = True
            dominant_side = "home"
        elif away_wins == 4:
            won_4_of_5 = True
            dominant_side = "away"

    if not (won_all or won_4_of_5):
        return None

    if recent_count < MIN_RECENT_MATCHES:
        return None

    return {
        "id": event.get("id"),
        "name": event.get("name"),
        "tournamentId": event.get("tournamentId"),
        "dominant_side": dominant_side,
        "home_wins": home_wins,
        "away_wins": away_wins,
        "h2h_total": total,
        "recent_matches": recent_count,
        "criteria": "won_all" if won_all else "won_4_of_5",
        "odds": event.get("odds"),
    }


def _pick_odds(pick):
    odds = pick.get("odds") or {}
    return odds.get(pick["dominant_side"])


def run(path="TT_today.json"):
    try:
        with open(path, "r", encoding="utf-8") as f:
            events = json.load(f)
    except FileNotFoundError:
        print(f"staker: {path} not found.")
        return []
    except json.JSONDecodeError:
        print(f"staker: {path} is not valid JSON.")
        return []

    picks = []
    for event in events:
        p = _analyze_event(event)
        if p:
            picks.append(p)

    if not picks:
        print("staker: no qualifying picks this cycle.")
        return []

    print(f"staker: {len(picks)} qualifying pick(s):")
    for p in picks:
        side = p["dominant_side"].upper()
        price = _pick_odds(p)
        price_str = f"@ {price}" if price else "no price"
        if p["criteria"] == "won_all":
            basis = f"won all {p['h2h_total']} h2h"
        else:
            basis = f"won {max(p['home_wins'], p['away_wins'])}/{p['h2h_total']} h2h"
        print(
            f"  {p['name']}  |  pick {side} {price_str}  |  "
            f"{basis}  |  {p['recent_matches']}/{p['h2h_total']} recent"
        )
    return picks


if __name__ == "__main__":
    run()