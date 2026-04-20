#!/usr/bin/env python3
"""
Garmin Training Advisor – Cloud Backend für Railway
Nutzt garminconnect 0.3.2 mit neuer Mobile-SSO Authentifizierung
"""

import json
import os
from datetime import date, timedelta
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from garminconnect import Garmin

app = Flask(__name__)
CORS(app)

# In-memory Token Cache
_client_cache = {}
TOKEN_DIR = "/tmp/garmin_tokens"
os.makedirs(TOKEN_DIR, exist_ok=True)

def token_path(email):
    safe = "".join(c for c in email if c.isalnum() or c in "-_")
    return os.path.join(TOKEN_DIR, f"{safe}.json")

def get_client(email, password):
    """
    1. RAM Cache
    2. Gespeicherter Token (neu: DI OAuth Bearer Format)
    3. Frischer Login via Mobile SSO
    """
    # 1. RAM Cache
    if email in _client_cache:
        try:
            c = _client_cache[email]
            _ = c.display_name
            print("✅ RAM Cache")
            return c
        except Exception:
            del _client_cache[email]

    # 2. GARMIN_TOKEN Env-Variable (gespeicherter Token)
    env_token = os.environ.get("GARMIN_TOKEN")
    if env_token:
        try:
            c = Garmin(email, password)
            c.garth.loads(env_token)
            _ = c.display_name
            _client_cache[email] = c
            print("✅ ENV Token")
            return c
        except Exception as e:
            print(f"ENV Token ungültig: {e}")

    # 3. Datei-Token
    tp = token_path(email)
    if os.path.exists(tp):
        try:
            c = Garmin(email, password)
            with open(tp) as f:
                c.garth.loads(f.read())
            _ = c.display_name
            _client_cache[email] = c
            print("✅ Datei Token")
            return c
        except Exception as e:
            print(f"Datei Token ungültig: {e}")
            os.remove(tp)

    # 4. Frischer Login (neue Mobile SSO)
    print("🔐 Frischer Login via Mobile SSO...")
    c = Garmin(email, password)
    c.login()
    try:
        with open(tp, "w") as f:
            f.write(c.garth.dumps())
    except Exception as e:
        print(f"Token speichern fehlgeschlagen: {e}")
    _client_cache[email] = c
    print(f"✅ Login erfolgreich: {c.display_name}")
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
        "running": ("Laufen", "🏃"), "cycling": ("Radfahren", "🚴"),
        "swimming": ("Schwimmen", "🏊"), "lap_swimming": ("Schwimmen", "🏊"),
        "strength_training": ("Kraft", "💪"), "yoga": ("Yoga", "🧘"),
        "walking": ("Gehen", "🚶"), "hiking": ("Wandern", "⛰️"),
        "indoor_cycling": ("Indoor Bike", "🚴"), "elliptical": ("Ellipse", "🏃"),
        "cardio": ("Cardio", "❤️"), "soccer": ("Fussball", "⚽"),
        "tennis": ("Tennis", "🎾"), "fitness_equipment": ("Fitness", "🏋️"),
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

# ══════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════

@app.route("/")
def index():
    return jsonify({"status": "Garmin Training Advisor API v2", "auth": "Mobile SSO"})

@app.route("/health")
def health():
    return jsonify({"ok": True})

@app.route("/data", methods=["POST"])
def data():
    body = request.get_json() or {}
    email = body.get("email", "").strip()
    password = body.get("password", "")
    if not email or not password:
        return jsonify({"ok": False, "error": "E-Mail und Passwort fehlen"}), 400
    try:
        client = get_client(email, password)
        sleep = fetch_sleep(client)
        training = fetch_training(client)
        # Token für nächsten Start speichern
        try:
            tp = token_path(email)
            with open(tp, "w") as f:
                f.write(client.garth.dumps())
        except Exception:
            pass
        return jsonify({"ok": True, "sleep": sleep, "training": training})
    except Exception as e:
        err = str(e)
        _client_cache.pop(email, None)
        if "429" in err or "Too Many" in err:
            return jsonify({"ok": False, "error": "Garmin 429 – bitte 30 Min warten"}), 429
        if "MFA" in err or "2FA" in err or "factor" in err.lower():
            return jsonify({"ok": False, "error": "2-Faktor-Auth aktiv – bitte in Garmin Connect deaktivieren"}), 401
        return jsonify({"ok": False, "error": err}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
