#!/usr/bin/env python3
"""
Garmin Training Advisor – Cloud Backend für Railway
"""

import json
import os
from datetime import date, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS

try:
    from garminconnect import Garmin
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "garminconnect"])
    from garminconnect import Garmin

app = Flask(__name__)
CORS(app)

# ─── In-memory Client Cache (überlebt mehrere Requests im selben Prozess) ───
_client_cache = {}   # email -> Garmin client
TOKEN_DIR = "/tmp/garmin_tokens"
os.makedirs(TOKEN_DIR, exist_ok=True)

def token_path(email):
    safe = "".join(c for c in email if c.isalnum() or c in "-_")
    return os.path.join(TOKEN_DIR, f"{safe}.json")

def get_client(email, password):
    """
    Gibt einen authentifizierten Garmin-Client zurück.
    Reihenfolge: 1) RAM-Cache  2) Datei-Token  3) Frischer Login
    Login wird nur einmal gemacht und dann gecacht.
    """
    # 1. RAM-Cache (schnellster Weg, kein Netzwerk)
    if email in _client_cache:
        try:
            c = _client_cache[email]
            _ = c.display_name   # prüfen ob Session noch aktiv
            print(f"✅ RAM-Cache verwendet für {email}")
            return c
        except Exception:
            del _client_cache[email]
            print("🔄 RAM-Cache abgelaufen")

    # 2. Gespeicherter Token (überlebt Railway-Sleeps)
    tp = token_path(email)
    if os.path.exists(tp):
        try:
            c = Garmin(email, password)
            with open(tp) as f:
                c.garth.loads(f.read())
            _ = c.display_name   # Validierung
            _client_cache[email] = c
            print(f"✅ Datei-Token verwendet für {email}")
            return c
        except Exception as e:
            print(f"🔄 Datei-Token ungültig: {e}")
            try:
                os.remove(tp)
            except Exception:
                pass

    # 3. Frischer Login (nur wenn kein gültiger Token vorhanden)
    print(f"🔐 Frischer Login für {email}")
    c = Garmin(email, password)
    c.login()
    # Token für nächste Mal speichern
    try:
        with open(tp, "w") as f:
            f.write(c.garth.dumps())
    except Exception as e:
        print(f"⚠️ Token konnte nicht gespeichert werden: {e}")
    _client_cache[email] = c
    return c

def to_hours(secs):
    return round((secs or 0) / 3600, 1)

def fetch_sleep(client, days=7):
    results = []
    today = date.today()
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        try:
            raw = client.get_sleep_data(d)
            dto = raw.get("dailySleepDTO", {})
            scores = dto.get("sleepScores", {})
            hrv_summary = raw.get("hrvSummary", {})
            score = None
            if isinstance(scores.get("overall"), dict):
                score = scores["overall"].get("value")
            elif scores.get("totalScore"):
                score = scores["totalScore"]
            hrv = hrv_summary.get("lastNight") or hrv_summary.get("weeklyAvg")
            entry = {
                "date": d,
                "duration": to_hours(dto.get("sleepTimeSeconds")),
                "deepSleep": to_hours(dto.get("deepSleepSeconds")),
                "remSleep": to_hours(dto.get("remSleepSeconds")),
                "lightSleep": to_hours(dto.get("lightSleepSeconds")),
                "score": score,
                "hrv": hrv,
                "restingHr": dto.get("restingHeartRate"),
            }
            if entry["duration"] > 0:
                results.append(entry)
        except Exception as e:
            print(f"Sleep {d}: {e}")
    return results

def fetch_training(client, days=7):
    TYPE_MAP = {
        "running":          ("Laufen",         "🏃"),
        "cycling":          ("Radfahren",       "🚴"),
        "swimming":         ("Schwimmen",       "🏊"),
        "lap_swimming":     ("Schwimmen",       "🏊"),
        "strength_training":("Kraft",           "💪"),
        "yoga":             ("Yoga",            "🧘"),
        "walking":          ("Gehen",           "🚶"),
        "hiking":           ("Wandern",         "⛰️"),
        "indoor_cycling":   ("Indoor Cycling",  "🚴"),
        "elliptical":       ("Ellipse",         "🏃"),
        "cardio":           ("Cardio",          "❤️"),
    }
    today = date.today()
    start = (today - timedelta(days=days)).isoformat()
    end = today.isoformat()
    try:
        activities = client.get_activities_by_date(start, end) or []
    except Exception as e:
        print(f"Activities: {e}")
        activities = []

    results = []
    existing = set()
    for a in activities:
        key = (a.get("activityType", {}).get("typeKey") or "").lower()
        name, emoji = TYPE_MAP.get(key, ("Training", "⚡"))
        d = (a.get("startTimeLocal") or "")[:10]
        existing.add(d)
        results.append({
            "date": d, "type": name, "emoji": emoji,
            "duration": round((a.get("duration") or 0) / 60),
            "load": round(a.get("activityTrainingLoad") or 0),
            "calories": round(a.get("calories") or 0),
            "distance": round((a.get("distance") or 0) / 1000, 2),
            "avgHr": a.get("averageHR"),
        })
    for i in range(days):
        d = (today - timedelta(days=i)).isoformat()
        if d not in existing:
            results.append({"date": d, "type": "Ruhetag", "emoji": "😴",
                            "duration": 0, "load": 0, "calories": 0})
    results.sort(key=lambda x: x["date"])
    return results[-days:]

# ─── Routes ───

@app.route("/")
def index():
    return jsonify({"status": "Garmin Training Advisor API", "version": "1.0"})

@app.route("/login", methods=["POST"])
def login():
    body = request.get_json() or {}
    email = body.get("email", "").strip()
    password = body.get("password", "")
    if not email or not password:
        return jsonify({"ok": False, "error": "E-Mail und Passwort fehlen"}), 400
    try:
        client = get_client(email, password)
        return jsonify({"ok": True, "user": client.display_name})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 401

@app.route("/data", methods=["POST"])
def data():
    body = request.get_json() or {}
    email = body.get("email", "").strip()
    password = body.get("password", "")
    if not email or not password:
        return jsonify({"ok": False, "error": "Zugangsdaten fehlen"}), 400
    try:
        client = get_client(email, password)
        sleep = fetch_sleep(client)
        training = fetch_training(client)
        return jsonify({"ok": True, "sleep": sleep, "training": training})
    except Exception as e:
        err = str(e)
        # Bei 429: Cache leeren damit nächster Versuch einen neuen Token holt
        if "429" in err or "Too Many Requests" in err:
            _client_cache.pop(email, None)
            tp = token_path(email)
            try:
                os.remove(tp)
            except Exception:
                pass
            return jsonify({
                "ok": False,
                "error": "Garmin hat zu viele Anfragen erkannt (429). Bitte 5–10 Minuten warten und es erneut versuchen."
            }), 429
        return jsonify({"ok": False, "error": err}), 500

@app.route("/health")
def health():
    return jsonify({"ok": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
