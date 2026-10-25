import json
import os
import time
import threading
import itertools
import requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

RECENT_WINDOW_DAYS = 30
MIN_RECENT_MATCHES = 2
MIN_STREAK_LENGTH = 3

_HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(os.path.dirname(_HERE), "tt_leadwith3.json")

BASE_URL = "https://m.betking.com"
OVERVIEW_URL = f"{BASE_URL}/en-ng/sports/live/api/overview/20"
OVERVIEW_DATA = "routes/($locale).sports.live.api.overview.$sportId"

EVENT_URL = f"{BASE_URL}/en-ng/sports/live/api/event"
EVENT_DATA = "routes/($locale).sports.live.api.event"

# --- Global State for Live Tracker ---
_pending_picks = {}
_active_fixtures = {}
_processed_fixtures = set()
_lock = threading.Lock()
_print_lock = threading.Lock()
_tracker_started = False
_executor = ThreadPoolExecutor(max_workers=10)
_worker_id_counter = itertools.count(1)
_show_logs = True


def set_logs(enabled):
    global _show_logs
    _show_logs = enabled


def _log(*args, **kwargs):
    if not _show_logs:
        return
    with _print_lock:
        print(*args, **kwargs, flush=True)


def is_tracking():
    with _lock:
        return _tracker_started


# ==========================================
# SUPABASE HELPERS
# ==========================================
def _load_db_config():
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "db.txt"),
        os.path.join(os.path.dirname(here), "db.txt"),
    ]
    for path in candidates:
        if os.path.exists(path):
            config = {}
            with open(path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, _, v = line.partition("=")
                        config[k.strip()] = v.strip().strip('"')
            return config
    return {}


def _get_supabase():
    try:
        from supabase import create_client
        config = _load_db_config()
        if not config:
            return None
        return create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])
    except Exception as e:
        _log(f"  [L3-DB Error] Supabase init failed: {e}")
        return None


def _get_headers(cookie):
    return {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://m.betking.com/en-ng/sports/live/table-tennis/20",
        "Cookie": cookie,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Gpc": "1",
    }


# ==========================================
# 1. PRE-MATCH ANALYSIS
# ==========================================

def _parse_date(date_str):
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%d-%m-%y")
    except ValueError:
        return None


def _last_n_wins(h2h, n, side):
    recent = h2h[:n]
    if len(recent) < n:
        return False
    return all(m.get("winner") == side for m in recent)


def _check_h2h(h2h):
    if not h2h:
        return None, None

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
        return None, None

    if recent_count < MIN_RECENT_MATCHES:
        return None, None

    if won_4_of_5 and not _last_n_wins(h2h, MIN_STREAK_LENGTH, dominant_side):
        return None, None

    criteria = "won_all" if won_all else "won_4_of_5"
    return dominant_side, criteria


def _check_last5(last5):
    if not last5:
        return False, None

    total = len(last5)
    wins = sum(1 for m in last5 if m.get("winner") == "win")

    if wins == total and total > 0:
        return True, "won_all"

    if total == 5 and wins == 4:
        if (last5[0].get("winner") == "win" and
            last5[1].get("winner") == "win" and
            last5[2].get("winner") == "win" and
            last5[3].get("winner") == "win" and
            last5[4].get("winner") == "lose"):
            return True, "won_4_of_5"

    return False, None


def _analyze_event(event):
    h2h = event.get("h2h") or []
    home_last5 = event.get("home_last5") or []
    away_last5 = event.get("away_last5") or []

    h2h_dom, h2h_crit = _check_h2h(h2h)
    if h2h_dom:
        return {
            "id": event.get("id"),
            "name": event.get("name"),
            "tournamentId": event.get("tournamentId"),
            "dominant_side": h2h_dom,
            "criteria": f"h2h_{h2h_crit}",
            "odds": event.get("odds"),
            "source": "h2h"
        }

    home_qual, home_crit = _check_last5(home_last5)
    if home_qual:
        return {
            "id": event.get("id"),
            "name": event.get("name"),
            "tournamentId": event.get("tournamentId"),
            "dominant_side": "home",
            "criteria": f"home_last5_{home_crit}",
            "odds": event.get("odds"),
            "source": "home_last5"
        }

    away_qual, away_crit = _check_last5(away_last5)
    if away_qual:
        return {
            "id": event.get("id"),
            "name": event.get("name"),
            "tournamentId": event.get("tournamentId"),
            "dominant_side": "away",
            "criteria": f"away_last5_{away_crit}",
            "odds": event.get("odds"),
            "source": "away_last5"
        }

    return None


# ==========================================
# 2. LIVE MATCH TRACKER WORKERS
# ==========================================

def _save_qualified_to_db(pick, period_scores, odds_val):
    sb = _get_supabase()
    if not sb:
        return

    record = {
        "team": pick["name"],
        "dominated": pick["dominant_side"],
        "reason": pick["criteria"],
        "qualified": f"{pick['dominant_side'].upper()} led by 3 points",
        "setscores": period_scores.replace(" - ", ", "),
        "Live Odds": str(odds_val) if odds_val else "N/A",
        "date": datetime.utcnow().strftime("%Y-%m-%d")
    }

    try:
        resp = sb.table("bet_arb").select("qualified_2").eq("id", 1).execute()
        if resp.data:
            existing = resp.data[0].get("qualified_2") or []
            if not isinstance(existing, list):
                existing = []
            
            existing.append(record)
            
            sb.table("bet_arb").update({"qualified_2": existing}).eq("id", 1).execute()
            _log(f"  [L3-DB] Saved qualified_2 record for {pick['name']}")
    except Exception as e:
        _log(f"  [L3-DB Error] Failed to save qualified_2 record: {e}")


def _check_lead_condition(set_str, dominant):
    try:
        h, a = map(int, set_str.split(":"))
    except ValueError:
        return False
    
    if dominant == "home":
        return h > 5 and (h - a) >= 3
    else:
        return a > 5 and (a - h) >= 3


def _track_match_live(pick, fixture_id, cookie, worker_id):
    name = pick["name"]
    dominant = pick["dominant_side"]
    _log(f"  [L3-worker{worker_id}] Started tracking lead by 3 for {name} (ID: {fixture_id})")

    while True:
        time.sleep(2)
        url = f"{EVENT_URL}?areaId=1&fixtureId={fixture_id}&_data={EVENT_DATA}"
        
        try:
            r = requests.get(url, headers=_get_headers(cookie), timeout=10)
            if r.status_code != 200:
                continue
            data = r.json()
        except Exception:
            continue

        event = data.get("event")
        if not event:
            _log(f"  [L3-worker{worker_id}] Match ended or removed: {name}")
            break

        period_scores = event.get("periodScores", "")
        sets = [s.strip() for s in period_scores.split(" - ")]

        if len(sets) >= 1:
            if _check_lead_condition(sets[0], dominant):
                odds_val = None
                for m in data.get("markets", []):
                    if m.get("marketTypeId") == 9388:
                        for sel in m.get("selections", []):
                            if (dominant == "home" and sel.get("name") == "1") or (dominant == "away" and sel.get("name") == "2"):
                                odds_val = sel.get("odd", {}).get("value")
                odds_str = f"@ {odds_val}" if odds_val else "(Odds unavailable)"
                _log(f"  [L3-worker{worker_id}] [L3-QUALIFIED] {name} | {dominant.upper()} led by 3 in Set 1 [{sets[0]}] | Live Odds: {odds_str}")
                _save_qualified_to_db(pick, sets[0], odds_val)
                break

        if len(sets) >= 2:
            if _check_lead_condition(sets[1], dominant):
                odds_val = None
                for m in data.get("markets", []):
                    if m.get("marketTypeId") == 9388:
                        for sel in m.get("selections", []):
                            if (dominant == "home" and sel.get("name") == "1") or (dominant == "away" and sel.get("name") == "2"):
                                odds_val = sel.get("odd", {}).get("value")
                odds_str = f"@ {odds_val}" if odds_val else "(Odds unavailable)"
                _log(f"  [L3-worker{worker_id}] [L3-QUALIFIED] {name} | {dominant.upper()} led by 3 in Set 2 [{sets[0]}, {sets[1]}] | Live Odds: {odds_str}")
                _save_qualified_to_db(pick, f"{sets[0]}, {sets[1]}", odds_val)
                break

            if len(sets) >= 3:
                _log(f"  [L3-worker{worker_id}] [L3-Dropped] {name} failed to lead by 3 in first 2 sets (scores: {period_scores})")
                break

    with _lock:
        _active_fixtures.pop(fixture_id, None)
        _processed_fixtures.add(pick["id"])


def _poll_overview_loop(cookie):
    while True:
        try:
            with _lock:
                if not _pending_picks:
                    current_pending = None
                else:
                    current_pending = dict(_pending_picks)

            if current_pending:
                r = requests.get(f"{OVERVIEW_URL}?_data={OVERVIEW_DATA}", headers=_get_headers(cookie), timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    live_events = []
                    for sport in data.get("sportData", []):
                        for tourney in sport.get("tournaments", []):
                            for ev in tourney.get("events", []):
                                live_events.append(ev)

                    for ev in live_events:
                        ev_name = ev.get("name", "").lower().replace(" vs ", " - ")
                        if ev_name in current_pending:
                            fixture_id = ev.get("id")
                            
                            with _lock:
                                if fixture_id in _active_fixtures:
                                    continue
                                
                                pick = _pending_picks.pop(ev_name, None)
                                if not pick:
                                    continue
                                
                                if pick["id"] in _processed_fixtures:
                                    continue

                                _active_fixtures[fixture_id] = pick

                            worker_id = next(_worker_id_counter)
                            _executor.submit(_track_match_live, pick, fixture_id, cookie, worker_id)
        except Exception:
            pass
        finally:
            time.sleep(60)


def add_picks(picks, cookie):
    global _tracker_started
    if not picks:
        return

    try:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(picks, f, indent=2, ensure_ascii=False)
    except Exception as e:
        _log(f"leadwith3: failed to save {OUTPUT_FILE} - {e}")

    new_added = []
    with _lock:
        active_ids = {p["id"] for p in _active_fixtures.values()}
        for p in picks:
            if p["id"] in _processed_fixtures or p["id"] in active_ids:
                continue
            live_name = p["name"].lower().replace(" vs ", " - ")
            if live_name not in _pending_picks:
                _pending_picks[live_name] = p
                new_added.append(p)

    if new_added:
        for p in new_added:
            side = p["dominant_side"].upper()
            _log(f"  [L3-Queued] {p['name']} | {side} | {p['criteria']}")

    if not _tracker_started:
        _tracker_started = True
        threading.Thread(target=_poll_overview_loop, args=(cookie,), daemon=True).start()


def analyze_file(path="TT_today.json"):
    try:
        with open(path, "r", encoding="utf-8") as f:
            events = json.load(f)
    except FileNotFoundError:
        _log(f"leadwith3: {path} not found.")
        return []
    except json.JSONDecodeError:
        _log(f"leadwith3: {path} is not valid JSON.")
        return []

    picks = []
    for event in events:
        p = _analyze_event(event)
        if p:
            picks.append(p)

    return picks