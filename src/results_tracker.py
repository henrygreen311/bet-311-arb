import os
import time
import threading
import requests
from concurrent.futures import ThreadPoolExecutor

_HERE = os.path.dirname(os.path.abspath(__file__))
EVENT_URL = "https://m.betking.com/en-ng/sports/live/api/event"
EVENT_DATA = "routes/($locale).sports.live.api.event"

_executor = ThreadPoolExecutor(max_workers=20)
_lock = threading.Lock()
_print_lock = threading.Lock()
_tracked_fixtures = set()


def _log(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs, flush=True)


def _get_supabase():
    try:
        import sys
        _ROOT = os.path.dirname(_HERE)
        if _ROOT not in sys.path:
            sys.path.append(_ROOT)
        from main import _get_supabase as main_get_supabase
        return main_get_supabase()
    except Exception as e:
        _log(f"  [Result Worker DB Error] {e}")
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


def _update_final_scores_in_db(team_name, finalscores, results):
    sb = _get_supabase()
    if not sb:
        return
    with _lock:
        try:
            resp = sb.table("bet_arb").select("qualified").eq("id", 1).execute()
            if resp.data:
                existing = resp.data[0].get("qualified") or []
                updated = False
                # Find the most recent record for this team
                for r in reversed(existing):
                    if r.get("team") == team_name:
                        r["finalscores"] = finalscores
                        r["results"] = results
                        updated = True
                        break
                if updated:
                    sb.table("bet_arb").update({"qualified": existing}).eq("id", 1).execute()
                    _log(f"  [Result Worker] DB updated final scores for {team_name}")
        except Exception as e:
            _log(f"  [Result Worker] DB Error: {e}")


def _track_results(pick, fixture_id, cookie):
    team_name = pick["name"]
    last_period_scores = None
    last_score = None

    while True:
        time.sleep(5)
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
            _log(f"  [Result Worker] Match ended: {team_name}")
            if last_period_scores and last_score:
                # Convert "6:11 - 8:11" to "6:11, 8:11"
                finals = last_period_scores.replace(" - ", ", ")
                # Convert "1:3" to "1-3"
                results = last_score.replace(":", "-")
                _update_final_scores_in_db(team_name, finals, results)
            break

        ps = event.get("periodScores")
        sc = event.get("score")
        if ps:
            last_period_scores = ps
        if sc:
            last_score = sc


def start_tracking(pick, fixture_id, cookie):
    with _lock:
        if fixture_id in _tracked_fixtures:
            return
        _tracked_fixtures.add(fixture_id)
    
    _log(f"  [Result Worker] Started tracking final results for {pick['name']} (ID: {fixture_id})")
    _executor.submit(_track_results, pick, fixture_id, cookie)
