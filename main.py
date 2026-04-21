#!/usr/bin/env python3
"""
Cycling Coach – Railway Backend
PostgreSQL + Garmin + Anthropic Claude
"""

import json
import os
import re
from datetime import date, timedelta, datetime
from flask import Flask, request, jsonify, Response, send_from_directory
from flask_cors import CORS
from garminconnect import Garmin
import requests as req
import psycopg
from psycopg.rows import dict_row

app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app)

# ══════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════

def get_db():
    conn = psycopg.connect(os.environ["DATABASE_URL"], sslmode="require")
    return conn

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS activities (
                    id BIGINT PRIMARY KEY,
                    date DATE,
                    name TEXT,
                    type TEXT,
                    duration_min INT,
                    avg_power INT,
                    norm_power INT,
                    max_power INT,
                    max_20min_power INT,
                    avg_hr INT,
                    max_hr INT,
                    calories INT,
                    training_load FLOAT,
                    aerobic_te FLOAT,
                    anaerobic_te FLOAT,
                    power_zones JSONB,
                    hr_zones JSONB,
                    laps JSONB,
                    raw JSONB,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS health_data (
                    date DATE PRIMARY KEY,
                    sleep_duration FLOAT,
                    deep_sleep FLOAT,
                    rem_sleep FLOAT,
                    sleep_score INT,
                    hrv INT,
                    resting_hr INT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS chat_messages (
                    id SERIAL PRIMARY KEY,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    activity_id BIGINT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS profile (
                    id INT PRIMARY KEY DEFAULT 1,
                    data JSONB,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS training_plan (
                    id SERIAL PRIMARY KEY,
                    week_start DATE NOT NULL,
                    plan JSONB NOT NULL,
                    generated_at TIMESTAMPTZ DEFAULT NOW(),
                    notes TEXT
                );
            """)
        conn.commit()
    print("✅ DB initialisiert")

# ══════════════════════════════════════════════
# GARMIN AUTH
# ══════════════════════════════════════════════

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
            c.login(tokenstore=tp)
            _ = c.display_name
            _client_cache[email] = c
            print("✅ Token geladen")
            return c
        except Exception as e:
            print(f"Token ungültig: {e}")
            try: os.remove(tp)
            except: pass

    print("🔐 Frischer Login...")
    c = Garmin(email, password)
    c.login(tokenstore=tp)
    _client_cache[email] = c
    print(f"✅ Eingeloggt: {c.display_name}")
    return c

# ══════════════════════════════════════════════
# GARMIN DATA FETCHING
# ══════════════════════════════════════════════

def to_hours(secs):
    return round((secs or 0) / 3600, 1)

def parse_laps(splits_raw):
    laps = splits_raw.get("lapDTOs", [])
    result = []
    for l in laps:
        dur = round((l.get("duration") or 0) / 60, 1)
        if dur < 0.5:
            continue
        result.append({
            "index": l.get("lapIndex"),
            "duration_min": dur,
            "avg_power": l.get("averagePower"),
            "norm_power": l.get("normalizedPower"),
            "max_power": l.get("maxPower"),
            "avg_hr": l.get("averageHR"),
            "max_hr": l.get("maxHR"),
            "cadence": l.get("averageBikeCadence"),
            "intensity": l.get("intensityType"),
        })
    return result

def sync_activities(client, days=30):
    """Holt Aktivitäten und speichert sie in DB."""
    today = date.today()
    start = (today - timedelta(days=days)).isoformat()
    activities = client.get_activities_by_date(start, today.isoformat()) or []

    saved = 0
    with get_db() as conn:
        with conn.cursor() as cur:
            for a in activities:
                aid = a.get("activityId")
                if not aid:
                    continue

                # Prüfen ob bereits vorhanden
                cur.execute("SELECT id FROM activities WHERE id=%s", (aid,))
                if cur.fetchone():
                    continue

                # Lap-Daten holen
                laps = []
                try:
                    splits = client.get_activity_splits(aid)
                    laps = parse_laps(splits)
                except Exception as e:
                    print(f"Laps {aid}: {e}")

                power_zones = {f"Z{i}": a.get(f"powerTimeInZone_{i}") for i in range(1, 8)}
                hr_zones = {f"Z{i}": a.get(f"hrTimeInZone_{i}") for i in range(1, 6)}

                cur.execute("""
                    INSERT INTO activities
                    (id, date, name, type, duration_min, avg_power, norm_power, max_power,
                     max_20min_power, avg_hr, max_hr, calories, training_load,
                     aerobic_te, anaerobic_te, power_zones, hr_zones, laps, raw)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO NOTHING
                """, (
                    aid,
                    a.get("startTimeLocal", "")[:10] or None,
                    a.get("activityName"),
                    a.get("activityType", {}).get("typeKey"),
                    round((a.get("duration") or 0) / 60),
                    a.get("avgPower"),
                    a.get("normPower"),
                    a.get("maxPower"),
                    a.get("maxAvgPower_20"),
                    a.get("averageHR"),
                    a.get("maxHR"),
                    a.get("calories"),
                    a.get("activityTrainingLoad"),
                    a.get("aerobicTrainingEffect"),
                    a.get("anaerobicTrainingEffect"),
                    json.dumps(power_zones),
                    json.dumps(hr_zones),
                    json.dumps(laps),
                    json.dumps({k: a.get(k) for k in ["activityName","duration","avgPower","normPower","averageHR"]})
                ))
                saved += 1
        conn.commit()
    print(f"✅ {saved} neue Aktivitäten gespeichert")
    return saved

def fetch_hrv_for_date(client, d):
    """Holt HRV für ein Datum — probiert mehrere Methoden."""
    # Methode 1: get_hrv_data
    try:
        hrv_data = client.get_hrv_data(d)
        if hrv_data:
            val = (hrv_data.get("hrvSummary", {}).get("lastNight")
                or hrv_data.get("hrvSummary", {}).get("lastNightAvg")
                or hrv_data.get("lastNight")
                or hrv_data.get("lastNightAvg")
                or hrv_data.get("weeklyAvg"))
            if val and float(val) > 0:
                print(f"HRV {d} via get_hrv_data: {val}")
                return round(float(val))
    except Exception as e:
        print(f"HRV method1 {d}: {e}")

    # Methode 2: aus Schlaf-hrvSummary
    try:
        sleep = client.get_sleep_data(d)
        hrv_s = sleep.get("hrvSummary", {})
        print(f"HRV fields {d}: {hrv_s}")
        val = (hrv_s.get("lastNight")
            or hrv_s.get("lastNightAvg")
            or hrv_s.get("lastNight5MinHigh"))
        if val and float(val) > 0:
            return round(float(val))
        # weeklyAvg als letzter Ausweg
        val = hrv_s.get("weeklyAvg")
        if val and float(val) > 0:
            return round(float(val))
    except Exception as e:
        print(f"HRV method2 {d}: {e}")

    return None

def sync_health(client, days=30):
    """Holt Schlaf/HRV und speichert in DB."""
    today = date.today()
    saved = 0
    with get_db() as conn:
        with conn.cursor() as cur:
            for i in range(days):
                d = (today - timedelta(days=i)).isoformat()
                try:
                    raw = client.get_sleep_data(d)
                    dto = raw.get("dailySleepDTO", {})
                    hrv_s = raw.get("hrvSummary", {})
                    scores = dto.get("sleepScores", {})
                    score = None
                    if isinstance(scores.get("overall"), dict):
                        score = scores["overall"].get("value")
                    elif scores.get("totalScore"):
                        score = scores["totalScore"]

                    # HRV: alle möglichen Felder probieren
                    hrv = (hrv_s.get("lastNight")
                        or hrv_s.get("lastNightAvg")
                        or hrv_s.get("lastNight5MinHigh"))

                    # Falls kein HRV aus Schlaf → separater Endpoint
                    if not hrv or float(hrv) <= 0:
                        hrv = fetch_hrv_for_date(client, d)
                    else:
                        hrv = round(float(hrv))

                    dur = to_hours(dto.get("sleepTimeSeconds"))
                    if dur > 0:
                        cur.execute("""
                            INSERT INTO health_data
                            (date, sleep_duration, deep_sleep, rem_sleep, sleep_score, hrv, resting_hr)
                            VALUES (%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (date) DO UPDATE SET
                            sleep_duration=EXCLUDED.sleep_duration,
                            hrv=COALESCE(EXCLUDED.hrv, health_data.hrv),
                            sleep_score=COALESCE(EXCLUDED.sleep_score, health_data.sleep_score),
                            deep_sleep=EXCLUDED.deep_sleep,
                            rem_sleep=EXCLUDED.rem_sleep,
                            resting_hr=COALESCE(EXCLUDED.resting_hr, health_data.resting_hr)
                        """, (d, dur, to_hours(dto.get("deepSleepSeconds")),
                              to_hours(dto.get("remSleepSeconds")), score,
                              hrv, dto.get("restingHeartRate")))
                        saved += 1
                        print(f"Health {d}: dur={dur}h score={score} hrv={hrv}")
                except Exception as e:
                    print(f"Health {d}: {e}")
        conn.commit()
    print(f"✅ {saved} Gesundheitsdaten gespeichert/aktualisiert")
    return saved

# ══════════════════════════════════════════════
# COACH LOGIC
# ══════════════════════════════════════════════

def build_context(profile, recent_activities, recent_health, chat_history):
    """Baut den kompletten Coach-Kontext für Claude."""
    ftp = profile.get("ftp", 210)
    weight = profile.get("weight", 63)
    wpkg = round(ftp / weight, 2)
    goal_wpkg = profile.get("goal_wpkg", 4.0)
    goal_ftp = round(goal_wpkg * weight)

    def classify_lap(lap, ftp):
        p = lap.get("avg_power") or 0
        pct = round(p / ftp * 100) if ftp else 0
        if pct < 56: zone = "Z1 (Erholung)"
        elif pct < 76: zone = "Z2 (Grundlage)"
        elif pct < 91: zone = "Z3 (Tempo/SST)"
        elif pct < 106: zone = "Z4 (Schwelle)"
        elif pct < 121: zone = "Z5 (VO2max)"
        elif pct < 151: zone = "Z6 (Anaerob)"
        else: zone = "Z7 (Neuromuskulär)"
        return f"{pct}% FTP → {zone}"

    acts_text = ""
    for i, a in enumerate(recent_activities[:10]):
        laps = a.get("laps") or []
        if isinstance(laps, str):
            try: laps = json.loads(laps)
            except: laps = []
        lap_text = ""
        for l in laps:
            if l.get("avg_power"):
                lap_text += f"\n      Lap {l['index']}: {l['duration_min']}min @ {l['avg_power']}W ({classify_lap(l, ftp)})"
        marker = " ← NEUESTES TRAINING" if i == 0 else ""
        acts_text += f"""
• {a['date']} – {a['name']}{marker}
  Dauer: {a['duration_min']}min | Ø {a['avg_power'] or '?'}W | NP: {a['norm_power'] or '?'}W | Ø HR: {a['avg_hr'] or '?'}bpm
  Aerob TE: {a['aerobic_te'] or '?'} | Anaerob TE: {a['anaerobic_te'] or '?'}{lap_text}"""

    health_text = ""
    for h in recent_health[:7]:
        health_text += f"\n• {h['date']}: Schlaf {h['sleep_duration']}h | Score {h['sleep_score'] or '?'} | HRV {h['hrv'] or '?'}ms | Ruhepuls {h['resting_hr'] or '?'}bpm"

    history_text = ""
    for m in chat_history[-20:]:
        role = "Du" if m["role"] == "user" else "Coach"
        history_text += f"\n{role}: {m['content']}"

    return f"""Du bist ein erfahrener Radsport-Coach. Stil: direkt, ehrlich, datenbasiert, motivierend.
WICHTIG: Du hast ALLE Trainingsdaten des Athleten unten. Du brauchst KEINE weiteren Daten anzufragen — analysiere direkt was du hast.

HEUTE: {date.today().strftime('%A, %d.%m.%Y')} (Wochentag beachten!)

ATHLETEN-PROFIL:
- FTP: {ftp}W | Gewicht: {weight}kg | Aktuell: {wpkg} W/kg
- Ziel: {goal_wpkg} W/kg = {goal_ftp}W FTP (noch +{goal_ftp - ftp}W)
- Trainingstage/Woche: {profile.get('days', 4)}

TRAININGS-ZONEN (FTP {ftp}W):
Z1 <{round(ftp*0.55)}W | Z2 {round(ftp*0.56)}-{round(ftp*0.75)}W | Z3 {round(ftp*0.76)}-{round(ftp*0.90)}W
Z4 {round(ftp*0.91)}-{round(ftp*1.05)}W | Z5 {round(ftp*1.06)}-{round(ftp*1.20)}W | Z6+ >{round(ftp*1.21)}W

LETZTE AKTIVITÄTEN (neueste zuerst):
{acts_text if acts_text else "Keine Aktivitäten gefunden — Sync durchführen."}

GESUNDHEITSDATEN (letzte 7 Tage):
{health_text if health_text else "Keine Gesundheitsdaten — Sync durchführen."}

BISHERIGER CHAT (nur zur Orientierung):
{history_text if history_text else "Neues Gespräch."}

Regeln:
- Frag NIEMALS nach Daten die du bereits oben hast
- Beziehe dich immer auf konkrete Zahlen aus den Daten
- Nenne immer konkrete Wattbereiche bei Empfehlungen
- Antworte auf Deutsch, präzise und ohne Fülltext"""

# ══════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/health")
def health():
    return jsonify({"ok": True})

@app.route("/init", methods=["POST"])
def init():
    try:
        init_db()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/sync", methods=["POST"])
def sync():
    body = request.get_json() or {}
    email = body.get("email", "").strip()
    password = body.get("password", "")
    days = body.get("days", 30)
    if not email or not password:
        return jsonify({"ok": False, "error": "Zugangsdaten fehlen"}), 400
    try:
        client = get_client(email, password)
        acts = sync_activities(client, days)
        health_saved = sync_health(client, days)
        return jsonify({"ok": True, "activities_saved": acts, "health_saved": health_saved})
    except Exception as e:
        _client_cache.pop(email, None)
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/dashboard", methods=["POST"])
def dashboard():
    """Gibt alle Dashboard-Daten zurück."""
    try:
        with get_db() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""
                    SELECT * FROM activities
                    ORDER BY date DESC LIMIT 20
                """)
                activities = [dict(r) for r in cur.fetchall()]

                cur.execute("""
                    SELECT * FROM health_data
                    ORDER BY date DESC LIMIT 7
                """)
                health = [dict(r) for r in cur.fetchall()]

                # Parse JSON fields
                for a in activities:
                    for f in ["laps", "power_zones", "hr_zones", "raw"]:
                        if isinstance(a.get(f), str):
                            try: a[f] = json.loads(a[f])
                            except: pass
                    # Serialize dates
                    if a.get("date"):
                        a["date"] = str(a["date"])
                    if a.get("created_at"):
                        a["created_at"] = str(a["created_at"])

                for h in health:
                    if h.get("date"):
                        h["date"] = str(h["date"])
                    if h.get("created_at"):
                        h["created_at"] = str(h["created_at"])

        return jsonify({"ok": True, "activities": activities, "health": health})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/profile", methods=["GET", "POST"])
def profile():
    if request.method == "POST":
        data = request.get_json() or {}
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO profile (id, data) VALUES (1, %s)
                        ON CONFLICT (id) DO UPDATE SET data=EXCLUDED.data, updated_at=NOW()
                    """, (json.dumps(data),))
                conn.commit()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    else:
        try:
            with get_db() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute("SELECT data FROM profile WHERE id=1")
                    row = cur.fetchone()
            return jsonify({"ok": True, "profile": row["data"] if row else {}})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/chat", methods=["POST"])
def chat():
    body = request.get_json() or {}
    message = body.get("message", "").strip()
    if not message:
        return jsonify({"ok": False, "error": "Nachricht fehlt"}), 400

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"ok": False, "error": "ANTHROPIC_API_KEY fehlt"}), 500

    try:
        with get_db() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                # Profil
                cur.execute("SELECT data FROM profile WHERE id=1")
                row = cur.fetchone()
                profile_data = row["data"] if row else {}

                # Letzte Aktivitäten
                cur.execute("""
                    SELECT id, date::text, name, duration_min, avg_power, norm_power,
                           avg_hr, aerobic_te, anaerobic_te, laps, power_zones, hr_zones,
                           training_load
                    FROM activities ORDER BY date DESC, created_at DESC LIMIT 15
                """)
                activities = [dict(r) for r in cur.fetchall()]
                for a in activities:
                    for f in ["laps", "power_zones", "hr_zones"]:
                        if isinstance(a.get(f), str):
                            try: a[f] = json.loads(a[f])
                            except: pass

                # Gesundheitsdaten
                cur.execute("""
                    SELECT date::text, sleep_duration, sleep_score, hrv, resting_hr
                    FROM health_data ORDER BY date DESC LIMIT 14
                """)
                health = [dict(r) for r in cur.fetchall()]

                # Chat-Verlauf
                cur.execute("""
                    SELECT role, content FROM chat_messages
                    ORDER BY created_at DESC LIMIT 30
                """)
                history = list(reversed([dict(r) for r in cur.fetchall()]))

        # Nachricht speichern
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO chat_messages (role, content) VALUES (%s, %s)",
                    ("user", message)
                )
            conn.commit()

        # Context aufbauen
        context = build_context(profile_data, activities, health, history)

        # Claude aufrufen — mit oder ohne Bild
        image_data = body.get("image_data")
        image_type = body.get("image_type", "image/jpeg")

        if image_data:
            # Mit Bild
            user_content = [
                {"type": "image", "source": {"type": "base64", "media_type": image_type, "data": image_data}},
                {"type": "text", "text": context + f"\n\nAthlet: {message}"}
            ]
        else:
            user_content = context + f"\n\nAthlet: {message}"

        messages = [{"role": "user", "content": user_content}]

        res = req.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1500,
                "messages": messages
            },
            timeout=45
        )
        data = res.json()
        reply = "".join(b.get("text", "") for b in data.get("content", []))
        if not reply:
            return jsonify({"ok": False, "error": f"Claude Fehler: {data}"}), 500

        # Antwort speichern
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO chat_messages (role, content) VALUES (%s, %s)",
                    ("assistant", reply)
                )
            conn.commit()

        return jsonify({"ok": True, "reply": reply})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/generate-plan", methods=["POST"])
def generate_plan():
    body = request.get_json() or {}
    start_date = body.get("start_date")  # ISO date string
    weeks = body.get("weeks", 4)
    training_days = body.get("training_days", [])  # e.g. ["Mon","Wed","Fri"]
    notes = body.get("notes", "")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"ok": False, "error": "ANTHROPIC_API_KEY fehlt"}), 500

    try:
        with get_db() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT data FROM profile WHERE id=1")
                row = cur.fetchone()
                profile_data = row["data"] if row else {}
                cur.execute("SELECT id, date::text, name, duration_min, avg_power, norm_power, laps FROM activities ORDER BY date DESC LIMIT 20")
                activities = [dict(r) for r in cur.fetchall()]
                cur.execute("SELECT date::text, sleep_score, hrv FROM health_data ORDER BY date DESC LIMIT 14")
                health = [dict(r) for r in cur.fetchall()]

        ftp = profile_data.get("ftp", 210)
        weight = profile_data.get("weight", 63)
        goal_wpkg = profile_data.get("goal_wpkg", 4.0)
        goal_ftp = round(goal_wpkg * weight)
        days_per_week = len(training_days) if training_days else profile_data.get("days", 4)

        day_names = {"Mon":"Montag","Tue":"Dienstag","Wed":"Mittwoch","Thu":"Donnerstag","Fri":"Freitag","Sat":"Samstag","Sun":"Sonntag"}
        days_text = ", ".join([day_names.get(d,d) for d in training_days]) if training_days else f"{days_per_week} Tage/Woche"

        acts_text = "\n".join([f"• {a['date']}: {a['name']} {a['duration_min']}min @ {a['avg_power'] or '?'}W" for a in activities[:10]])
        health_text = "\n".join([f"• {h['date']}: Score {h['sleep_score'] or '?'} HRV {h['hrv'] or '?'}ms" for h in health[:7]])

        prompt = f"""Erstelle einen {weeks}-Wochen Trainingsplan für einen Radsportler. Antworte NUR mit einem JSON-Objekt, kein anderer Text.

PROFIL:
- FTP: {ftp}W | Gewicht: {weight}kg | Aktuell: {ftp/weight:.2f} W/kg
- Ziel: {goal_wpkg} W/kg = {goal_ftp}W FTP (noch +{goal_ftp-ftp}W)
- Trainingstage: {days_text}
- Planstart: {start_date}

LETZTE TRAININGS:
{acts_text}

GESUNDHEIT (letzte Woche):
{health_text}

ZUSÄTZLICHE HINWEISE: {notes if notes else "keine"}

Trainings-Zonen (FTP {ftp}W):
Z1 <{round(ftp*0.55)}W | Z2 {round(ftp*0.56)}-{round(ftp*0.75)}W | Z3 {round(ftp*0.76)}-{round(ftp*0.9)}W | Z4 {round(ftp*0.91)}-{round(ftp*1.05)}W | Z5 >{round(ftp*1.06)}W

Erstelle den Plan als JSON in diesem Format:
{{
  "goal": "Kurze Beschreibung des Planziels",
  "weeks": [
    {{
      "week": 1,
      "start": "YYYY-MM-DD",
      "focus": "Grundlage aufbauen",
      "days": [
        {{
          "date": "YYYY-MM-DD",
          "day": "Montag",
          "type": "SST",
          "title": "Sweet Spot 2x20",
          "duration_min": 75,
          "description": "Aufwärmen 15min, 2x20min @ {round(ftp*0.88)}-{round(ftp*0.93)}W (Z3/SST), 10min Cool-down",
          "target_power": "{round(ftp*0.88)}-{round(ftp*0.93)}W",
          "intensity": "mittel",
          "rest": false
        }},
        {{
          "date": "YYYY-MM-DD",
          "day": "Dienstag",
          "type": "rest",
          "title": "Ruhetag",
          "duration_min": 0,
          "description": "Aktive Erholung oder komplett frei",
          "target_power": null,
          "intensity": "keine",
          "rest": true
        }}
      ]
    }}
  ]
}}

Plane nur die angegebenen Trainingstage als Trainings, alle anderen als Ruhetage.
Starte mit dem Datum {start_date} für Woche 1.
Variiere die Einheiten sinnvoll: Z2 Grundlage, SST, Schwellenintervalle, VO2max — je nach Woche und Periodisierung.
Antworte NUR mit dem JSON, kein Text davor oder danach."""

        res = req.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 4000, "messages": [{"role": "user", "content": prompt}]},
            timeout=60
        )
        data = res.json()
        text = "".join(b.get("text", "") for b in data.get("content", []))

        # Parse JSON
        text_clean = text.strip()
        if text_clean.startswith("```"):
            text_clean = text_clean.split("\n", 1)[1].rsplit("```", 1)[0]
        plan_data = json.loads(text_clean)

        # Speichern
        start = date.fromisoformat(start_date)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM training_plan WHERE week_start >= %s", (start,))
                cur.execute("INSERT INTO training_plan (week_start, plan, notes) VALUES (%s, %s, %s)",
                           (start, json.dumps(plan_data), notes))
            conn.commit()

        return jsonify({"ok": True, "plan": plan_data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/get-plan", methods=["GET"])
def get_plan():
    try:
        with get_db() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT plan, generated_at::text, notes FROM training_plan ORDER BY generated_at DESC LIMIT 1")
                row = cur.fetchone()
        if not row:
            return jsonify({"ok": True, "plan": None})
        plan = row["plan"] if isinstance(row["plan"], dict) else json.loads(row["plan"])
        return jsonify({"ok": True, "plan": plan, "generated_at": row["generated_at"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/adapt-plan", methods=["POST"])
def adapt_plan():
    """Coach passt den Plan basierend auf einer Nachricht an."""
    body = request.get_json() or {}
    message = body.get("message", "")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    try:
        with get_db() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT plan FROM training_plan ORDER BY generated_at DESC LIMIT 1")
                row = cur.fetchone()
                if not row:
                    return jsonify({"ok": False, "error": "Kein Plan vorhanden"}), 404
                cur.execute("SELECT data FROM profile WHERE id=1")
                prof_row = cur.fetchone()
                profile_data = prof_row["data"] if prof_row else {}

        plan = row["plan"] if isinstance(row["plan"], dict) else json.loads(row["plan"])
        ftp = profile_data.get("ftp", 210)

        prompt = f"""Du bist ein Radsport-Coach. Passe den folgenden Trainingsplan basierend auf der Anfrage des Athleten an.
Antworte mit einem JSON-Objekt im gleichen Format wie der bestehende Plan.

ANFRAGE: {message}

BESTEHENDER PLAN:
{json.dumps(plan, ensure_ascii=False, indent=2)[:3000]}

FTP: {ftp}W

Passe nur die nötigen Tage an. Antworte NUR mit dem aktualisierten JSON."""

        res = req.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 4000, "messages": [{"role": "user", "content": prompt}]},
            timeout=60
        )
        data = res.json()
        text = "".join(b.get("text", "") for b in data.get("content", []))
        text_clean = text.strip()
        if text_clean.startswith("```"):
            text_clean = text_clean.split("\n", 1)[1].rsplit("```", 1)[0]
        updated_plan = json.loads(text_clean)

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE training_plan SET plan=%s, notes=%s WHERE id=(SELECT id FROM training_plan ORDER BY generated_at DESC LIMIT 1)",
                           (json.dumps(updated_plan), f"Angepasst: {message}"))
            conn.commit()

        return jsonify({"ok": True, "plan": updated_plan})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/clear-chat", methods=["POST"])
def clear_chat():
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM chat_messages")
            conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/history", methods=["GET"])
def history():
    """Gibt den kompletten Chat-Verlauf zurück."""
    try:
        with get_db() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""
                    SELECT role, content, created_at::text
                    FROM chat_messages ORDER BY created_at ASC
                """)
                messages = [dict(r) for r in cur.fetchall()]
        return jsonify({"ok": True, "messages": messages})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    try:
        init_db()
    except Exception as e:
        print(f"DB init: {e}")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
