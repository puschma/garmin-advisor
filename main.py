#!/usr/bin/env python3
"""
Garmin Training Advisor – Cloud Backend für Railway
garminconnect 0.3.2 + Mobile SSO + KI-Analyse über Server
"""

import json
import os
import requests as req
from datetime import date, timedelta
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from garminconnect import Garmin

app = Flask(__name__)
CORS(app)

_client_cache = {}
TOKEN_DIR = "/tmp/garmin_tokens"
os.makedirs(TOKEN_DIR, exist_ok=True)

def token_path(email):
    safe = "".join(c for c in email if c.isalnum() or c in "-_")
    return os.path.join(TOKEN_DIR, f"{safe}.json")

def get_client(email, password):
    if email in _client_cache:
        try:
            c = _client_cache[email]
            _ = c.display_name
            return c
        except Exception:
            del _client_cache[email]

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
            print(f"Token ungültig: {e}")
            os.remove(tp)

    print("🔐 Frischer Login via Mobile SSO...")
    c = Garmin(email, password)
    c.login()
    try:
        with open(tp, "w") as f:
            f.write(c.garth.dumps())
    except Exception as e:
        print(f"Token speichern: {e}")
    _client_cache[email] = c
    print(f"✅ Eingeloggt: {c.display_name}")
    return c

def to_hours(secs):
    return round((secs or 0) / 3600, 1)

def fetch_hrv(client, d):
    """Holt HRV-Wert für ein bestimmtes Datum – probiert mehrere Methoden."""
    try:
        # Methode 1: get_hrv_data (neuere API)
        hrv = client.get_hrv_data(d)
        if hrv:
            val = (hrv.get("hrvSummary", {}).get("lastNight")
                or hrv.get("hrvSummary", {}).get("weeklyAvg")
                or hrv.get("lastNight")
                or hrv.get("weeklyAvg")
                or hrv.get("lastNightAvg"))
            if val:
                return round(float(val))
    except Exception as e:
        print(f"HRV method 1 {d}: {e}")

    try:
        # Methode 2: aus Schlaf-Daten
        sleep = client.get_sleep_data(d)
        hrv_s = sleep.get("hrvSummary", {})
        val = hrv_s.get("lastNight") or hrv_s.get("weeklyAvg")
        if val:
            return round(float(val))
        # Methode 3: aus dailySleepDTO
        dto = sleep.get("dailySleepDTO", {})
        val = dto.get("avgSleepStress")
        if val:
            # Stress invertiert zu HRV (Näherung)
            return max(10, round(100 - float(val)))
    except Exception as e:
        print(f"HRV method 2 {d}: {e}")

    try:
        # Methode 4: get_rhr_day (Ruheherzrate als Proxy)
        rhr = client.get_rhr_day(d, d)
        if rhr and isinstance(rhr, list) and len(rhr) > 0:
            val = rhr[0].get("value") or rhr[0].get("restingHeartRate")
            # RHR ist kein HRV aber besser als nichts
            # Gib None zurück – lieber leer als falsch
    except Exception as e:
        print(f"HRV method 3 {d}: {e}")

    return None

def fetch_sleep(client, days=7):
    results = []
    today = date.today()
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        try:
            raw = client.get_sleep_data(d)
            dto = raw.get("dailySleepDTO", {})
            scores = dto.get("sleepScores", {})

            score = None
            if isinstance(scores.get("overall"), dict):
                score = scores["overall"].get("value")
            elif scores.get("totalScore"):
                score = scores["totalScore"]
            elif isinstance(scores.get("totalScore"), (int, float)):
                score = scores["totalScore"]

            # HRV: Priorität lastNight > lastNightAvg > weeklyAvg
            hrv_summary = raw.get("hrvSummary", {})
            hrv = (hrv_summary.get("lastNight")
                or hrv_summary.get("lastNightAvg")
                or hrv_summary.get("lastNight5MinHigh")
                or hrv_summary.get("weeklyAvg"))

            # Debug: alle HRV-Felder loggen
            print(f"HRV fields {d}: {json.dumps({k:v for k,v in hrv_summary.items() if v is not None})}")

            if not hrv:
                hrv = fetch_hrv(client, d)

            if hrv:
                hrv = round(float(hrv))

            print(f"Sleep {d}: dur={to_hours(dto.get('sleepTimeSeconds'))}h score={score} hrv={hrv}")

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
    return jsonify({"status": "Garmin Training Advisor API v2"})

@app.route("/debug-activity", methods=["POST"])
def debug_activity():
    body = request.get_json() or {}
    email = body.get("email", "").strip()
    password = body.get("password", "")
    try:
        client = get_client(email, password)
        today = date.today()
        start = (today - timedelta(days=14)).isoformat()
        activities = client.get_activities_by_date(start, today.isoformat()) or []

        cycling = [a for a in activities if any(k in (a.get("activityType", {}).get("typeKey") or "").lower()
                   for k in ["cycl", "virtual", "zwift"])]
        if not cycling:
            return jsonify({"ok": False, "error": "Keine Radeinheit gefunden"})

        last = cycling[0]
        activity_id = last.get("activityId")

        result = {
            "activity_name": last.get("activityName"),
            "date": last.get("startTimeLocal", "")[:10],
            "duration_min": round((last.get("duration") or 0) / 60),
            "avg_power": last.get("avgPower"),
            "norm_power": last.get("normPower"),
            "max_power": last.get("maxPower"),
            "max_20min_power": last.get("maxAvgPower_20"),
            "avg_hr": last.get("averageHR"),
            "power_zones": {f"Z{i}": last.get(f"powerTimeInZone_{i}") for i in range(1, 8)},
            "hr_zones": {f"Z{i}": last.get(f"hrTimeInZone_{i}") for i in range(1, 6)},
            "aerobic_te": last.get("aerobicTrainingEffect"),
            "anaerobic_te": last.get("anaerobicTrainingEffect"),
            "splits": [],
            "intervals": [],
        }

        # Splits holen
        try:
            splits = client.get_activity_splits(activity_id)
            result["splits_raw"] = splits
            # Versuche relevante Lap-Daten zu extrahieren
            laps = splits.get("lapDTOs") or splits.get("laps") or splits if isinstance(splits, list) else []
            result["splits"] = [{
                "lap": i+1,
                "duration_min": round((l.get("duration") or l.get("elapsedDuration") or 0) / 60, 1),
                "avg_power": l.get("averagePower") or l.get("avgPower"),
                "avg_hr": l.get("averageHR"),
                "distance_km": round((l.get("distance") or 0) / 1000, 2),
            } for i, l in enumerate(laps[:20])]
        except Exception as e:
            result["splits_error"] = str(e)

        # Intensity Intervals
        try:
            intervals = client.get_activity_intensity_intervals(activity_id)
            result["intervals_raw_keys"] = list(intervals.keys()) if isinstance(intervals, dict) else str(type(intervals))
            iv_list = intervals.get("intervalDTOs") or intervals.get("intervals") or []
            result["intervals"] = [{
                "type": iv.get("intensityType") or iv.get("type"),
                "duration_min": round((iv.get("duration") or 0) / 60, 1),
                "avg_power": iv.get("averagePower") or iv.get("avgPower"),
                "avg_hr": iv.get("averageHR"),
            } for iv in iv_list[:20]]
        except Exception as e:
            result["intervals_error"] = str(e)

        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/health")
def health():
    return jsonify({"ok": True})

@app.route("/debug-page")
def debug_page():
    html = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Debug</title>
<style>body{background:#000;color:#fff;font-family:monospace;padding:24px}
input{width:100%;padding:10px;background:#111;border:1px solid #333;color:#fff;border-radius:8px;margin-bottom:10px;font-size:14px}
button{padding:12px 24px;background:#0a84ff;border:none;border-radius:8px;color:#fff;font-size:15px;cursor:pointer}
pre{background:#111;padding:16px;border-radius:8px;margin-top:16px;overflow-x:auto;font-size:12px;line-height:1.5;white-space:pre-wrap}
</style></head><body>
<h2>Garmin Activity Debug</h2>
<input id="email" type="email" placeholder="Garmin E-Mail">
<input id="pw" type="password" placeholder="Garmin Passwort">
<button onclick="run()">Letzte Radeinheit analysieren</button>
<pre id="out">Ergebnis erscheint hier...</pre>
<script>
async function run(){
  document.getElementById('out').textContent='Lade... (kann 20-30s dauern)';
  try{
    const res=await fetch('/debug-activity',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email:document.getElementById('email').value,password:document.getElementById('pw').value})});
    const d=await res.json();
    document.getElementById('out').textContent=JSON.stringify(d,null,2);
  }catch(e){document.getElementById('out').textContent='Fehler: '+e.message;}
}
</script></body></html>"""
    return Response(html, mimetype='text/html')

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
        try:
            with open(token_path(email), "w") as f:
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
            return jsonify({"ok": False, "error": "2-Faktor-Auth aktiv – bitte in Garmin deaktivieren"}), 401
        return jsonify({"ok": False, "error": err}), 500

@app.route("/analyze", methods=["POST"])
def analyze():
    body = request.get_json() or {}
    sleep = body.get("sleep", [])
    training = body.get("training", [])
    profile = body.get("profile", {})

    if not sleep:
        return jsonify({"ok": False, "error": "Keine Schlafdaten"}), 400

    today = sleep[-1] if sleep else {}
    hrv_vals = [s["hrv"] for s in sleep if s.get("hrv")]
    avg_hrv = round(sum(hrv_vals) / len(hrv_vals)) if hrv_vals else 0
    total_load = sum(t.get("load", 0) for t in training)

    profile_text = ""
    if profile:
        profile_text = f"""
ATHLETEN-PROFIL:
- Ziel: {profile.get('goal', 'nicht angegeben')}
- Fitness-Level: {profile.get('level', 'nicht angegeben')}
- Alter: {profile.get('age', '?')} Jahre
- Gewicht: {profile.get('weight', '?')} kg
- Geschlecht: {profile.get('gender', '?')}
- Geplante Trainingstage/Woche: {profile.get('days', '?')}
"""

    prompt = f"""Du bist ein erfahrener Personal Trainer und Schlafmediziner. Erstelle eine hochpersonalisierte Trainingsanalyse auf Deutsch.
{profile_text}
SCHLAFDATEN (letzte 7 Tage):
{chr(10).join(f"• {s['date']}: {s['duration']}h | Tief {s['deepSleep']}h | REM {s['remSleep']}h | Score {s.get('score') or '?'} | HRV {s.get('hrv') or '?'}ms" for s in sleep)}

TRAININGSDATEN (letzte 7 Tage):
{chr(10).join(f"• {t['date']}: {t['type']} {t['duration']}min | Load {t['load']} | {t['calories']}kcal" for t in training)}

KENNZAHLEN:
- HRV heute: {today.get('hrv') or '?'}ms | Ø 7 Tage: {avg_hrv}ms
- Wochenbelastung: {total_load} ATL

Berücksichtige das Athleten-Profil bei ALLEN Empfehlungen.

🔋 ERHOLUNGSSTATUS
Bewerte die aktuelle Erholung konkret mit Datenbezug.

🏋️ EMPFEHLUNG FÜR HEUTE
Konkretes Training: Typ, Dauer, Intensität passend zum Ziel und Fitness-Level. Warum genau das?

📅 WOCHENPLAN
{profile.get('days', 4)}-Tage-Plan passend zum Ziel "{profile.get('goal', 'Gesund bleiben')}".

😴 SCHLAF-OPTIMIERUNG
2–3 datenbasierte Tipps.

⚡ WICHTIGSTE ERKENNTNIS
Die eine Sache die dieser Athlet jetzt wissen muss."""

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return jsonify({"ok": False, "error": "GEMINI_API_KEY nicht gesetzt. Bitte in Railway Variables eintragen."}), 500

    try:
        res = req.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-lite:generateContent?key={api_key}",
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 1200, "temperature": 0.7}
            },
            timeout=30
        )
        data = res.json()
        if "error" in data:
            return jsonify({"ok": False, "error": f"Gemini Fehler: {data['error'].get('message', str(data))}"}), 500
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return jsonify({"ok": True, "text": text})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
