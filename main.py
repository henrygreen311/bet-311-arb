import json
import os
import secrets
import sys
import time

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import analyzer


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
    raise FileNotFoundError(f"db.txt not found in any of: {candidates}")


def _get_supabase():
    from supabase import create_client
    config = _load_db_config()
    return create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])


BASE = "https://m.betking.com"
TODAY_URL = f"{BASE}/en-ng/sports/table-tennis/today"
TODAY_DATA = "routes/($locale).sports.$sportName.$tabTitle"
SPORTRADAR_URL = f"{BASE}/en-ng/sports/home/api/prematch/sportsradar/match"
SR_WIDGET_ROOT = "https://widgets.fn.sportradar.com/common/en/Etc:UTC/gismo"
SR_MATCH_INFO_PATH = "match_info"
SR_H2H_PATH = "stats_team_versusrecent"
SR_LASTX_PATH = "stats_team_lastx"
SR_SPORT_ID = 20
REFERER = f"{BASE}/en-ng/sports/table-tennis/competitions"
ROW_ID = 1
OUTPUT_FILE = os.path.join(_HERE, "TT_today.json")
H2H_LIMIT = 5
SCAN_INTERVAL = 5 * 60


def _betking_headers(cookie):
    return {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": REFERER,
        "Traceparent": f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-00",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Gpc": "1",
        "Priority": "u=0",
        "Cookie": cookie,
    }


def _sr_headers():
    return {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        "Origin": "https://m.betking.com",
        "Referer": "https://m.betking.com/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Gpc": "1",
    }


def _extract_events(payload):
    for widget in payload.get("widgetsData") or []:
        event_list = ((widget or {}).get("data") or {}).get("eventListData") or {}
        events = event_list.get("events")
        if isinstance(events, list) and events:
            return events
    return []


def _summarize(event):
    raw_name = event.get("name") or ""
    raw_date = event.get("date") or ""
    
    date_str = ""
    time_str = ""
    if raw_date:
        parts = raw_date.split("T")
        if len(parts) == 2:
            date_str = parts[0]
            time_str = parts[1].replace("Z", "")

    return {
        "id": event.get("id"),
        "name": raw_name.replace(" - ", " vs "),
        "categoryId": event.get("categoryId"),
        "tournamentId": event.get("tournamentId"),
        "date": date_str,
        "time": time_str,
    }


def _extract_odds(event):
    markets = event.get("markets") or []
    if not markets:
        return None
    selections = (markets[0] or {}).get("selections") or []
    home = None
    away = None
    for sel in selections:
        if not isinstance(sel, dict):
            continue
        name = sel.get("name")
        value = (sel.get("odd") or {}).get("value")
        if name == "1" and home is None:
            home = value
        elif name == "2" and away is None:
            away = value
    if home is None or away is None:
        return None
    return {"home": str(home), "away": str(away)}


def _fetch_sportradar(cookie, match_id):
    try:
        r = requests.get(
            SPORTRADAR_URL,
            params={"matchId": match_id},
            headers=_betking_headers(cookie),
            timeout=15,
        )
    except requests.RequestException as e:
        return None, str(e)
    if r.status_code >= 400:
        return None, f"HTTP {r.status_code}"
    try:
        return r.json(), None
    except ValueError:
        return None, "invalid JSON"


def _fetch_sr_widget(token, path):
    url = f"{SR_WIDGET_ROOT}/{path}?T={token}"
    try:
        r = requests.get(url, headers=_sr_headers(), timeout=15)
    except requests.RequestException as e:
        return None, str(e)
    if r.status_code >= 400:
        return None, f"HTTP {r.status_code}"
    try:
        return r.json(), None
    except ValueError:
        return None, "invalid JSON"


def _extract_uids(payload):
    for d in payload.get("doc") or []:
        match = ((d or {}).get("data") or {}).get("match") or {}
        teams = match.get("teams") or {}
        home_uid = (teams.get("home") or {}).get("uid")
        away_uid = (teams.get("away") or {}).get("uid")
        if home_uid is not None and away_uid is not None:
            return home_uid, away_uid
    return None, None


def _extract_h2h(payload, current_home_uid, current_away_uid):
    results = []
    for d in payload.get("doc") or []:
        data = (d or {}).get("data") or {}
        for match in data.get("matches") or []:
            ft = ((match.get("periods") or {}).get("ft")) or {}
            home_score = ft.get("home")
            away_score = ft.get("away")
            if home_score is None or away_score is None:
                continue

            teams = match.get("teams") or {}
            h2h_home_uid = (teams.get("home") or {}).get("uid")

            if h2h_home_uid == current_home_uid:
                current_home_is_h2h_home = True
            elif h2h_home_uid == current_away_uid:
                current_home_is_h2h_home = False
            else:
                continue

            if home_score == away_score:
                winner = "draw"
            elif (home_score > away_score) == current_home_is_h2h_home:
                winner = "home"
            else:
                winner = "away"

            date_raw = (match.get("time") or {}).get("date") or ""
            results.append({
                "ft": f"{home_score}-{away_score}",
                "date": date_raw.replace("/", "-"),
                "winner": winner,
            })
            if len(results) >= H2H_LIMIT:
                return results
    return results


def _extract_team_lastx(payload, tracked_uid, tracked_is_home):
    results = []
    for d in payload.get("doc") or []:
        data = (d or {}).get("data") or {}
        for match in data.get("matches") or []:
            ft = ((match.get("periods") or {}).get("ft")) or {}
            home_score = ft.get("home")
            away_score = ft.get("away")
            if home_score is None or away_score is None:
                continue

            teams = match.get("teams") or {}
            match_home_uid = (teams.get("home") or {}).get("uid")
            match_away_uid = (teams.get("away") or {}).get("uid")

            if match_home_uid == tracked_uid:
                tracked_is_historical_home = True
            elif match_away_uid == tracked_uid:
                tracked_is_historical_home = False
            else:
                continue

            if home_score == away_score:
                winner = "draw"
            else:
                tracked_won = (home_score > away_score) == tracked_is_historical_home
                winner = "win" if tracked_won else "lose"

            date_raw = (match.get("time") or {}).get("date") or ""
            results.append({
                "ft": f"{home_score}-{away_score}",
                "date": date_raw.replace("/", "-"),
                "winner": winner,
            })
            if len(results) >= H2H_LIMIT:
                return results
    return results


def scan_once():
    print("Fetching today's table tennis matches...")
    try:
        sb = _get_supabase()
        resp = (
            sb.table("bet_arb")
            .select("cookie,sr_token")
            .eq("id", ROW_ID)
            .limit(1)
            .execute()
        )
    except Exception as e:
        print(f"Error: supabase unavailable - {e}")
        return False, None

    if not resp.data:
        print(f"Error: no row with id={ROW_ID} in bet_arb.")
        return False, None
    row = resp.data[0]
    cookie = (row.get("cookie") or "").strip()
    sr_token = (row.get("sr_token") or "").strip()
    if not cookie:
        print(f"Error: cookie is empty for id={ROW_ID}.")
        return False, None
    if not sr_token:
        print(f"Error: sr_token is empty for id={ROW_ID}.")
        return False, None

    try:
        r = requests.get(
            TODAY_URL,
            params={"_data": TODAY_DATA},
            headers=_betking_headers(cookie),
            timeout=30,
        )
    except requests.RequestException as e:
        print(f"Error: request failed - {e}")
        return False, None
    if r.status_code >= 400:
        print(f"Error: server returned HTTP {r.status_code}")
        return False, None

    try:
        data = r.json()
    except ValueError:
        print("Error: response is not valid JSON.")
        return False, None

    events = _extract_events(data)
    if not events:
        print("No matches found today.")
        return False, cookie

    # Load existing data to prevent duplicate API calls and keep previous matches
    existing_data = {}
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                for ev in json.load(f):
                    existing_data[ev["id"]] = ev
        except Exception:
            pass

    summary = []
    new_count = 0
    print(f"Found {len(events)} matches from API.")
    print("Looking up sportradar data for new matches...")

    for event in events:
        row = _summarize(event)
        
        # If we already processed this match, just update odds and skip the heavy API calls
        if row["id"] in existing_data:
            existing_data[row["id"]]["odds"] = _extract_odds(event)
            continue

        new_count += 1
        sr, err = _fetch_sportradar(cookie, row["id"])
        if not (isinstance(sr, dict) and sr.get("sportRadarMatchId")):
            row["sportRadarMatchId"] = None
            row["moreStatistics"] = None
            row["homeUid"] = None
            row["awayUid"] = None
            row["odds"] = _extract_odds(event)
            row["h2h"] = None
            row["home_last5"] = None
            row["away_last5"] = None
            print(f"  {row['id']}: skipped ({err or 'no sportradar match'})")
            summary.append(row)
            continue

        row["sportRadarMatchId"] = sr.get("sportRadarMatchId")
        row["moreStatistics"] = sr.get("moreStatistics")

        info, ierr = _fetch_sr_widget(
            sr_token, f"{SR_MATCH_INFO_PATH}/{row['sportRadarMatchId']}"
        )
        if not isinstance(info, dict):
            row["homeUid"] = None
            row["awayUid"] = None
            row["odds"] = _extract_odds(event)
            row["h2h"] = None
            row["home_last5"] = None
            row["away_last5"] = None
            print(f"  {row['id']}: match_info failed ({ierr})")
            summary.append(row)
            continue

        home_uid, away_uid = _extract_uids(info)
        row["homeUid"] = home_uid
        row["awayUid"] = away_uid
        row["odds"] = _extract_odds(event)

        if home_uid is None or away_uid is None:
            row["h2h"] = None
            row["home_last5"] = None
            row["away_last5"] = None
            print(f"  {row['id']}: uids not found")
            summary.append(row)
            continue

        h2h_payload, herr = _fetch_sr_widget(
            sr_token, f"{SR_H2H_PATH}/{home_uid}/{away_uid}"
        )
        if not isinstance(h2h_payload, dict):
            row["h2h"] = None
            print(f"  {row['id']}: h2h failed ({herr})")
        else:
            row["h2h"] = _extract_h2h(h2h_payload, home_uid, away_uid)

        home_lastx_payload, hlerr = _fetch_sr_widget(
            sr_token, f"{SR_LASTX_PATH}/{home_uid}/{SR_SPORT_ID}"
        )
        if not isinstance(home_lastx_payload, dict):
            row["home_last5"] = None
            print(f"  {row['id']}: home_last5 failed ({hlerr})")
        else:
            row["home_last5"] = _extract_team_lastx(
                home_lastx_payload, home_uid, tracked_is_home=True
            )

        away_lastx_payload, alerr = _fetch_sr_widget(
            sr_token, f"{SR_LASTX_PATH}/{away_uid}/{SR_SPORT_ID}"
        )
        if not isinstance(away_lastx_payload, dict):
            row["away_last5"] = None
            print(f"  {row['id']}: away_last5 failed ({alerr})")
        else:
            row["away_last5"] = _extract_team_lastx(
                away_lastx_payload, away_uid, tracked_is_home=False
            )

        summary.append(row)

    # Merge existing matches with newly processed ones
    final_data = list(existing_data.values()) + summary

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(final_data, f, indent=2, ensure_ascii=False)

    print(f"Done. {new_count} new matches processed, {len(existing_data)} previously saved.")
    print(f"Total matches saved to {OUTPUT_FILE}: {len(final_data)}")
    return True, cookie


def main():
    cycle = 0
    while True:
        cycle += 1
        print(f"\n=== Cycle {cycle} ===")
        
        ok, cookie = scan_once()

        if ok and cookie:
            try:
                picks = analyzer.analyze_file(OUTPUT_FILE)
                if picks:
                    analyzer.add_picks(picks, cookie)
            except Exception as e:
                print(f"Error in analyzer: {e}")
        else:
            print("Scan failed - skipping analyzer this cycle.")

        print(f"Waiting {SCAN_INTERVAL // 60} minutes for next cycle...")
        try:
            time.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt:
            print("\nStopped.")
            break


if __name__ == "__main__":
    main()