#!/usr/bin/env python3
"""
Cycling Coach – Railway Backend
PostgreSQL + Garmin + Anthropic Claude
"""

import json
import os
import re
import time
from datetime import date, timedelta, datetime
from flask import Flask, request, jsonify, Response, send_from_directory, redirect
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

                CREATE TABLE IF NOT EXISTS strava_tokens (
                    id INT PRIMARY KEY DEFAULT 1,
                    access_token TEXT,
                    refresh_token TEXT,
                    expires_at BIGINT,
                    athlete_id BIGINT,
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
# STRAVA
# ══════════════════════════════════════════════

STRAVA_CLIENT_ID = os.environ.get("STRAVA_CLIENT_ID", "")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET", "")
STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API = "https://www.strava.com/api/v3"

def get_strava_token():
    """Holt gültigen Strava Access Token, refresht wenn nötig."""
    with get_db() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM strava_tokens WHERE id=1")
            row = cur.fetchone()
    if not row:
        return None
    # Token refresh wenn abgelaufen
    if row["expires_at"] < int(time.time()) + 60:
        res = req.post(STRAVA_TOKEN_URL, data={
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": row["refresh_token"]
        })
        data = res.json()
        if "access_token" in data:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE strava_tokens SET access_token=%s, refresh_token=%s,
                        expires_at=%s, updated_at=NOW() WHERE id=1
                    """, (data["access_token"], data["refresh_token"], data["expires_at"]))
                conn.commit()
            return data["access_token"]
        return None
    return row["access_token"]

def is_outdoor(name, activity_type):
    """Erkennt ob eine Aktivität outdoor ist."""
    name_lower = (name or "").lower()
    type_lower = (activity_type or "").lower()
    if "zwift" in name_lower or "virtual" in type_lower or "indoor" in name_lower:
        return False
    return True

def sync_strava(days=30):
    """Holt Outdoor-Aktivitäten von Strava und updated die DB."""
    token = get_strava_token()
    if not token:
        return 0, "Strava nicht verbunden"

    after = int(time.time()) - days * 86400
    headers = {"Authorization": f"Bearer {token}"}

    # Aktivitätsliste holen
    res = req.get(f"{STRAVA_API}/athlete/activities",
                  headers=headers,
                  params={"after": after, "per_page": 50})
    activities = res.json()
    if not isinstance(activities, list):
        return 0, f"Strava Fehler: {activities}"

    saved = 0
    with get_db() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            for a in activities:
                # Nur Radfahren, nur Outdoor
                if a.get("type") not in ("Ride", "VirtualRide"):
                    continue
                if a.get("type") == "VirtualRide":
                    continue  # Indoor → Garmin kümmert sich

                aid = a.get("id")
                if not aid:
                    continue

                # Prüfe ob schon vorhanden mit Strava-Daten
                cur.execute("SELECT id, raw FROM activities WHERE id=%s", (aid,))
                existing = cur.fetchone()
                if existing:
                    raw = existing["raw"]
                    if isinstance(raw, str):
                        try: raw = json.loads(raw)
                        except: raw = {}
                    if raw and raw.get("strava_synced"):
                        continue  # Bereits mit Strava-Daten

                # Detail-Daten holen (Zonen!)
                detail = req.get(f"{STRAVA_API}/activities/{aid}",
                                headers=headers).json()

                # Zonen holen
                zones_res = req.get(f"{STRAVA_API}/activities/{aid}/zones",
                                   headers=headers).json()

                # Power Zones extrahieren
                power_zones = {}
                if isinstance(zones_res, list):
                    for zone_group in zones_res:
                        if zone_group.get("type") == "power":
                            for i, z in enumerate(zone_group.get("distribution_buckets", []), 1):
                                power_zones[f"Z{i}"] = z.get("time", 0)

                # HR Zones
                hr_zones = {}
                if isinstance(zones_res, list):
                    for zone_group in zones_res:
                        if zone_group.get("type") == "heartrate":
                            for i, z in enumerate(zone_group.get("distribution_buckets", []), 1):
                                hr_zones[f"Z{i}"] = z.get("time", 0)

                # Laps/Segmente als Laps
                laps_res = req.get(f"{STRAVA_API}/activities/{aid}/laps",
                                  headers=headers).json()
                laps = []
                if isinstance(laps_res, list):
                    for i, l in enumerate(laps_res):
                        dur = round((l.get("elapsed_time") or 0) / 60, 1)
                        if dur < 0.5: continue
                        laps.append({
                            "index": i + 1,
                            "duration_min": dur,
                            "avg_power": l.get("average_watts"),
                            "max_power": l.get("max_watts"),
                            "avg_hr": l.get("average_heartrate"),
                            "cadence": l.get("average_cadence"),
                            "intensity": None,
                        })

                act_date = (a.get("start_date_local") or "")[:10]
                duration_min = round((a.get("moving_time") or 0) / 60)
                raw_data = {"strava_synced": True, "strava_id": aid}

                if existing:
                    cur.execute("""
                        UPDATE activities SET
                        power_zones=%s, hr_zones=%s, laps=%s, raw=%s,
                        avg_power=%s, norm_power=%s, avg_hr=%s
                        WHERE id=%s
                    """, (
                        json.dumps(power_zones), json.dumps(hr_zones),
                        json.dumps(laps), json.dumps(raw_data),
                        detail.get("average_watts"),
                        detail.get("weighted_average_watts"),
                        detail.get("average_heartrate"),
                        aid
                    ))
                else:
                    cur.execute("""
                        INSERT INTO activities
                        (id, date, name, type, duration_min, avg_power, norm_power,
                         avg_hr, max_hr, calories, power_zones, hr_zones, laps, raw)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (id) DO UPDATE SET
                        power_zones=EXCLUDED.power_zones,
                        hr_zones=EXCLUDED.hr_zones,
                        laps=EXCLUDED.laps,
                        raw=EXCLUDED.raw
                    """, (
                        aid, act_date, a.get("name"), "cycling",
                        duration_min,
                        detail.get("average_watts"),
                        detail.get("weighted_average_watts"),
                        detail.get("average_heartrate"),
                        detail.get("max_heartrate"),
                        detail.get("calories"),
                        json.dumps(power_zones), json.dumps(hr_zones),
                        json.dumps(laps), json.dumps(raw_data)
                    ))
                saved += 1
                print(f"Strava sync: {a.get('name')} {act_date} zones={power_zones}")
        conn.commit()
    return saved, "ok"


@app.route("/debug-strava")
def debug_strava():
    try:
        token = get_strava_token()
        if not token:
            return jsonify({"error": "Nicht verbunden"})
        headers = {"Authorization": f"Bearer {token}"}
        
        # Athleten-Info inkl. FTP
        athlete = req.get(f"{STRAVA_API}/athlete", headers=headers).json()
        
        # Letzte Aktivität mit Zonen
        acts = req.get(f"{STRAVA_API}/athlete/activities", 
                      headers=headers, params={"per_page": 3}).json()
        
        zones_info = {}
        if acts and isinstance(acts, list):
            aid = acts[0]["id"]
            zones_info = req.get(f"{STRAVA_API}/activities/{aid}/zones", 
                               headers=headers).json()
        
        return jsonify({
            "athlete_ftp": athlete.get("ftp"),
            "athlete_weight": athlete.get("weight"),
            "latest_activity": acts[0]["name"] if acts else None,
            "zones_raw": zones_info
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/init-strava", methods=["POST"])
def init_strava():
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS strava_tokens (
                        id INT PRIMARY KEY DEFAULT 1,
                        access_token TEXT,
                        refresh_token TEXT,
                        expires_at BIGINT,
                        athlete_id BIGINT,
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
            conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/strava-connect")
def strava_connect():
    """Leitet zu Strava OAuth weiter."""
    base_url = request.host_url.rstrip("/")
    redirect_uri = f"{base_url}/strava-callback"
    url = (f"{STRAVA_AUTH_URL}?client_id={STRAVA_CLIENT_ID}"
           f"&response_type=code&redirect_uri={redirect_uri}"
           f"&approval_prompt=force&scope=activity:read_all")
    return redirect(url)


@app.route("/strava-callback")
def strava_callback():
    """Verarbeitet Strava OAuth Callback."""
    try:
        code = request.args.get("code")
        error = request.args.get("error")
        print(f"Strava callback: code={code[:10] if code else None} error={error}")

        if error or not code:
            return f"<h2>❌ Strava Fehler: {error}</h2>", 400

        base_url = request.host_url.rstrip("/")
        redirect_uri = f"{base_url}/strava-callback"
        print(f"Token exchange: redirect_uri={redirect_uri}")

        res = req.post(STRAVA_TOKEN_URL, data={
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code"
        }, timeout=15)
        data = res.json()
        print(f"Token response keys: {list(data.keys())}")

        if "access_token" not in data:
            return f"<h2>❌ Token Fehler: {data}</h2>", 400

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO strava_tokens (id, access_token, refresh_token, expires_at, athlete_id)
                    VALUES (1, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                    access_token=EXCLUDED.access_token,
                    refresh_token=EXCLUDED.refresh_token,
                    expires_at=EXCLUDED.expires_at,
                    athlete_id=EXCLUDED.athlete_id,
                    updated_at=NOW()
                """, (data["access_token"], data["refresh_token"],
                      data["expires_at"], data.get("athlete", {}).get("id")))
            conn.commit()

        athlete = data.get("athlete", {})
        name = f"{athlete.get('firstname','')} {athlete.get('lastname','')}".strip()
        return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
        <style>body{{background:#0a0a0a;color:#f0f0f0;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
        .box{{text-align:center;padding:40px;background:#161616;border-radius:24px}}
        a{{padding:14px 28px;background:#4f8ef7;border-radius:12px;color:#fff;text-decoration:none;font-weight:700;display:inline-block;margin-top:20px}}</style>
        </head><body><div class="box">
        <div style="font-size:48px;margin-bottom:16px">✅</div>
        <h2>Strava verbunden!</h2>
        <p style="color:rgba(240,240,240,0.5)">Willkommen {name}!</p>
        <a href="/">Zurück zur App</a>
        </div></body></html>"""
    except Exception as e:
        print(f"Strava callback error: {e}")
        import traceback
        traceback.print_exc()
        return f"<h2>❌ Server Fehler: {str(e)}</h2>", 500


@app.route("/strava-status")
def strava_status():
    """Prüft ob Strava verbunden ist."""
    try:
        token = get_strava_token()
        if not token:
            return jsonify({"connected": False})
        with get_db() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT athlete_id FROM strava_tokens WHERE id=1")
                row = cur.fetchone()
        return jsonify({"connected": True, "athlete_id": row["athlete_id"] if row else None})
    except Exception as e:
        return jsonify({"connected": False, "error": str(e)})



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
                    ON CONFLICT (id) DO UPDATE SET
                    max_20min_power=EXCLUDED.max_20min_power,
                    laps=EXCLUDED.laps
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

                    # Ruhepuls: liegt im raw root, nicht im dailySleepDTO
                    resting_hr = (raw.get("restingHeartRate")
                        or dto.get("restingHeartRate"))
                    if not resting_hr:
                        try:
                            stats = client.get_stats(d)
                            resting_hr = stats.get("restingHeartRate")
                        except Exception as e:
                            print(f"get_stats {d}: {e}")

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
                            resting_hr=EXCLUDED.resting_hr
                        """, (d, dur, to_hours(dto.get("deepSleepSeconds")),
                              to_hours(dto.get("remSleepSeconds")), score,
                              hrv, resting_hr))
                        saved += 1
                        print(f"Health {d}: dur={dur}h score={score} hrv={hrv} rhr={resting_hr}")
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

    def format_zones(power_zones, hr_zones, duration_min, ftp):
        """Formatiert Zonen-Verteilung als lesbaren Text."""
        if not power_zones:
            return ""
        
        def fmt_sec(s):
            if not s or s == 0: return None
            m = round(s / 60)
            return f"{m}min" if m > 0 else None

        z_labels = {
            "Z1": f"Z1 Erholung <{round(ftp*0.55)}W",
            "Z2": f"Z2 Grundlage {round(ftp*0.56)}-{round(ftp*0.75)}W",
            "Z3": f"Z3 Tempo/SST {round(ftp*0.76)}-{round(ftp*0.90)}W",
            "Z4": f"Z4 Schwelle {round(ftp*0.91)}-{round(ftp*1.05)}W",
            "Z5": f"Z5 VO2max {round(ftp*1.06)}-{round(ftp*1.20)}W",
            "Z6": f"Z6+ Anaerob >{round(ftp*1.21)}W",
        }

        parts = []
        for z in ["Z1","Z2","Z3","Z4","Z5","Z6"]:
            secs = power_zones.get(z)
            t = fmt_sec(secs)
            if t:
                parts.append(f"{z_labels[z]}: {t}")

        return "\n      Zonen: " + " | ".join(parts) if parts else ""

    def detect_activity_type(name, activity_type):
        """Erkennt ob Indoor oder Outdoor."""
        name_lower = (name or "").lower()
        type_lower = (activity_type or "").lower()
        if "zwift" in name_lower or "virtual" in type_lower or "indoor" in name_lower:
            return "🏠 Indoor (Zwift)"
        elif "wahoo" in name_lower or any(x in name_lower for x in ["radfahren", "ride", "outdoor", "straße"]):
            return "🌳 Outdoor"
        return "🚴 Radfahren"

    acts_text = ""
    for i, a in enumerate(recent_activities[:10]):
        laps = a.get("laps") or []
        if isinstance(laps, str):
            try: laps = json.loads(laps)
            except: laps = []

        power_zones = a.get("power_zones") or {}
        if isinstance(power_zones, str):
            try: power_zones = json.loads(power_zones)
            except: power_zones = {}

        lap_text = ""
        for l in laps:
            if l.get("avg_power"):
                hr_str = f" | HR {l['avg_hr']}bpm" if l.get("avg_hr") else ""
                cad_str = f" | Kadenz {l['cadence']}" if l.get("cadence") else ""
                lap_text += f"\n      Lap {l['index']}: {l['duration_min']}min @ {l['avg_power']}W ({classify_lap(l, ftp)}){hr_str}{cad_str}"

        zones_text = format_zones(power_zones, a.get("hr_zones"), a.get("duration_min"), ftp)
        act_type = detect_activity_type(a.get("name"), a.get("type"))
        marker = " ← NEUESTES TRAINING" if i == 0 else ""

        # Outdoor-Hinweis wenn keine Laps aber Zonen vorhanden
        outdoor_note = ""
        if not laps and "🌳" in act_type and zones_text:
            outdoor_note = "\n      ⚠️ Outdoor ohne Laps — bitte Zonen-Verteilung für echte Intensitätsbewertung nutzen!"

        acts_text += f"""
• {a['date']} – {a['name']} [{act_type}]{marker}
  Dauer: {a['duration_min']}min | Ø {a['avg_power'] or '?'}W | NP: {a['norm_power'] or '?'}W | Ø HR: {a['avg_hr'] or '?'}bpm
  Aerob TE: {a['aerobic_te'] or '?'} | Anaerob TE: {a['anaerobic_te'] or '?'}{zones_text}{outdoor_note}{lap_text}"""

    health_text = ""
    for h in recent_health[:7]:
        health_text += f"\n• {h['date']}: Schlaf {h['sleep_duration']}h | Score {h['sleep_score'] or '?'} | HRV {h['hrv'] or '?'}ms | Ruhepuls {h['resting_hr'] or '?'}bpm"

    history_text = ""
    for m in chat_history[-20:]:
        role = "Du" if m["role"] == "user" else "Coach"
        history_text += f"\n{role}: {m['content']}"

    return f"""Du bist ein erfahrener Radsport-Coach. Stil: direkt, ehrlich, datenbasiert, motivierend.

⚠️ ABSOLUT WICHTIG: Du hast ALLE Daten bereits unten. Frage NIEMALS nach weiteren Daten, Screenshots oder Links. Analysiere was du hast — jetzt, direkt, ohne Rückfragen.

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
- Bei Outdoor-Einheiten ohne Laps: Nutze die Zonen-Verteilung für die Intensitätsbewertung — NICHT nur den Durchschnittswatt! Z.B. 45min in Z4/Z5 = intensive Einheit, egal ob Ø-Watt niedrig ist
- Bei Indoor/Zwift: Lap-Daten sind präziser, nutze diese
- Nenne immer konkrete Wattbereiche bei Empfehlungen
- Antworte auf Deutsch, präzise und ohne Fülltext"""

# ══════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/debug-health", methods=["GET"])
def debug_health():
    """Zeigt rohe Garmin-Daten für Diagnose."""
    email = request.args.get("email","")
    password = request.args.get("pw","")
    try:
        client = get_client(email, password)
        today = date.today().isoformat()
        raw = client.get_sleep_data(today)
        dto = raw.get("dailySleepDTO", {})

        # Alle möglichen RHR-Felder
        rhr_fields = {
            "dto.restingHeartRate": dto.get("restingHeartRate"),
            "dto.averageRestingHeartRate": dto.get("averageRestingHeartRate"),
            "raw.restingHeartRate": raw.get("restingHeartRate"),
            "raw.averageRestingHeartRate": raw.get("averageRestingHeartRate"),
            "dto keys with heart": [k for k in dto.keys() if "heart" in k.lower() or "hr" in k.lower()],
            "raw keys with heart": [k for k in raw.keys() if "heart" in k.lower() or "hr" in k.lower()],
        }

        # Stats für heute
        try:
            stats = client.get_stats(today)
            rhr_fields["stats.restingHeartRate"] = stats.get("restingHeartRate")
            rhr_fields["stats keys with heart"] = [k for k in stats.keys() if "heart" in k.lower() or "resting" in k.lower()]
        except Exception as e:
            rhr_fields["stats_error"] = str(e)

        # Letzte Radeinheit — 20min Power Felder
        acts = client.get_activities_by_date(
            (date.today()-timedelta(days=14)).isoformat(), today) or []
        cycling = [a for a in acts if "cycl" in (a.get("activityType",{}).get("typeKey","")).lower()
                   or "virtual" in (a.get("activityType",{}).get("typeKey","")).lower()]
        power_fields = {}
        if cycling:
            a = cycling[0]
            power_fields = {
                "name": a.get("activityName"),
                "maxAvgPower_20": a.get("maxAvgPower_20"),
                "maxAvgPower_1": a.get("maxAvgPower_1"),
                "maxAvgPower_2": a.get("maxAvgPower_2"),
                "maxAvgPower_5": a.get("maxAvgPower_5"),
                "normPower": a.get("normPower"),
                "avgPower": a.get("avgPower"),
                "all_power_keys": [k for k in a.keys() if "power" in k.lower() or "Power" in k],
            }

        return jsonify({
            "rhr_debug": rhr_fields,
            "power_debug": power_fields,
            "dto_all_keys": list(dto.keys()),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


    return jsonify({"ok": True})

@app.route("/fix-health", methods=["POST"])
def fix_health():
    """Löscht alle health_data und synct neu."""
    body = request.get_json() or {}
    email = body.get("email","")
    password = body.get("password","")
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM health_data")
            conn.commit()
        print(f"Health data deleted, starting sync for {email}")
        _client_cache.pop(email, None)
        tp = token_path(email)
        if os.path.exists(tp):
            os.remove(tp)
        client = get_client(email, password)
        print(f"Got client: {client.display_name}")
        # Test: hol einen Tag direkt
        today = date.today().isoformat()
        try:
            raw = client.get_sleep_data(today)
            print(f"Sleep data keys: {list(raw.keys())}")
            print(f"RHR: {raw.get('restingHeartRate')}")
            dto = raw.get("dailySleepDTO", {})
            print(f"Sleep duration: {dto.get('sleepTimeSeconds')}")
        except Exception as e:
            print(f"Sleep test error: {e}")
        saved = sync_health(client, 30)
        return jsonify({"ok": True, "saved": saved})
    except Exception as e:
        print(f"fix-health error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/debug-db")
def debug_db():
    try:
        with get_db() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT date::text, resting_hr, hrv, sleep_score FROM health_data ORDER BY date DESC LIMIT 5")
                health = [dict(r) for r in cur.fetchall()]
                cur.execute("SELECT date::text, name, max_20min_power, avg_power FROM activities ORDER BY date DESC LIMIT 5")
                acts = [dict(r) for r in cur.fetchall()]
        return jsonify({"health": health, "activities": acts})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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

        # Strava sync für Outdoor-Aktivitäten
        strava_saved = 0
        strava_msg = ""
        strava_token = get_strava_token()
        if strava_token:
            strava_saved, strava_msg = sync_strava(days)
            print(f"Strava: {strava_saved} Aktivitäten aktualisiert")

        return jsonify({
            "ok": True,
            "activities_saved": acts,
            "health_saved": health_saved,
            "strava_saved": strava_saved,
            "strava_connected": bool(strava_token)
        })
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

                # Historische Aktivitäten für Fortschritts-Chart (90 Tage)
                cur.execute("""
                    SELECT date::text, avg_power, norm_power, max_20min_power,
                           training_load, duration_min, type
                    FROM activities
                    WHERE date >= NOW() - INTERVAL '90 days'
                    AND avg_power IS NOT NULL
                    ORDER BY date ASC
                """)
                history = [dict(r) for r in cur.fetchall()]

                # Parse JSON fields
                for a in activities:
                    for f in ["laps", "power_zones", "hr_zones", "raw"]:
                        if isinstance(a.get(f), str):
                            try: a[f] = json.loads(a[f])
                            except: pass
                    if a.get("date"): a["date"] = str(a["date"])
                    if a.get("created_at"): a["created_at"] = str(a["created_at"])

                for h in health:
                    if h.get("date"): h["date"] = str(h["date"])
                    if h.get("created_at"): h["created_at"] = str(h["created_at"])

        return jsonify({"ok": True, "activities": activities, "health": health, "history": history})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/weekly-review", methods=["POST"])
def weekly_review():
    """Generiert einen Wochenrückblick vom Coach."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"ok": False, "error": "ANTHROPIC_API_KEY fehlt"}), 500
    try:
        with get_db() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT data FROM profile WHERE id=1")
                row = cur.fetchone()
                profile_data = row["data"] if row else {}

                # Letzte 7 Tage Aktivitäten
                cur.execute("""
                    SELECT date::text, name, duration_min, avg_power, norm_power,
                           avg_hr, training_load, aerobic_te, anaerobic_te, laps
                    FROM activities
                    WHERE date >= NOW() - INTERVAL '7 days'
                    ORDER BY date DESC
                """)
                week_acts = [dict(r) for r in cur.fetchall()]

                # Vorwoche zum Vergleich
                cur.execute("""
                    SELECT date::text, name, duration_min, avg_power, training_load
                    FROM activities
                    WHERE date >= NOW() - INTERVAL '14 days'
                    AND date < NOW() - INTERVAL '7 days'
                    ORDER BY date DESC
                """)
                prev_acts = [dict(r) for r in cur.fetchall()]

                # Gesundheitsdaten letzte 7 Tage
                cur.execute("""
                    SELECT date::text, sleep_duration, sleep_score, hrv, resting_hr
                    FROM health_data
                    WHERE date >= NOW() - INTERVAL '7 days'
                    ORDER BY date DESC
                """)
                week_health = [dict(r) for r in cur.fetchall()]

                # Parse laps
                for a in week_acts:
                    if isinstance(a.get("laps"), str):
                        try: a["laps"] = json.loads(a["laps"])
                        except: a["laps"] = []

        ftp = profile_data.get("ftp", 210)
        weight = profile_data.get("weight", 63)
        goal_wpkg = profile_data.get("goal_wpkg", 4.0)
        goal_ftp = round(goal_wpkg * weight)

        week_load = sum(a.get("training_load", 0) or 0 for a in week_acts)
        prev_load = sum(a.get("training_load", 0) or 0 for a in prev_acts)
        avg_hrv = round(sum(h["hrv"] for h in week_health if h.get("hrv")) / max(1, sum(1 for h in week_health if h.get("hrv")))) if week_health else 0
        avg_sleep = round(sum(h["sleep_duration"] for h in week_health if h.get("sleep_duration")) / max(1, sum(1 for h in week_health if h.get("sleep_duration"))), 1) if week_health else 0

        acts_text = "\n".join([
            f"• {a['date']}: {a['name']} — {a['duration_min']}min @ {a['avg_power'] or '?'}W | Load {round(a['training_load'] or 0)}"
            for a in week_acts
        ])

        prompt = f"""Du bist ein Radsport-Coach. Schreibe einen prägnanten Wochenrückblick auf Deutsch.

ATHLET: FTP {ftp}W | {ftp/weight:.2f} W/kg | Ziel: {goal_wpkg} W/kg = {goal_ftp}W

DIESE WOCHE:
{acts_text if acts_text else "Keine Aktivitäten"}
Gesamtbelastung: {round(week_load)} ATL
Vorwoche: {round(prev_load)} ATL ({'+' if week_load >= prev_load else ''}{round(week_load-prev_load)} ATL)

GESUNDHEIT:
Ø HRV: {avg_hrv}ms | Ø Schlaf: {avg_sleep}h

Struktur deines Rückblicks:
📊 WOCHE IN ZAHLEN — 2-3 Kerndaten
✅ WAS GUT WAR — konkret mit Datenbezug
⚠️ WAS FEHLT / VERBESSERUNG — ehrlich, direkt
🎯 FOKUS NÄCHSTE WOCHE — 1-2 konkrete Empfehlungen mit Wattbereichen

Maximal 200 Wörter. Direkt, kein Blabla."""

        res = req.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 600,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30
        )
        data = res.json()
        text = "".join(b.get("text", "") for b in data.get("content", []))
        if not text:
            return jsonify({"ok": False, "error": f"Claude: {data}"}), 500
        return jsonify({"ok": True, "review": text})
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
                    SELECT id, date::text, name, type, duration_min, avg_power, norm_power,
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

Erstelle den Plan als JSON. Jede Trainingseinheit braucht ein "intervals" Array mit genauen Segmenten für Zwift:

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
          "rest": false,
          "intervals": [
            {{"type": "warmup", "duration_sec": 900, "power_low": {round(ftp*0.45)}, "power_high": {round(ftp*0.65)}, "label": "Aufwärmen"}},
            {{"type": "work", "duration_sec": 1200, "power": {round(ftp*0.90)}, "label": "SST Block 1"}},
            {{"type": "rest", "duration_sec": 300, "power": {round(ftp*0.50)}, "label": "Erholung"}},
            {{"type": "work", "duration_sec": 1200, "power": {round(ftp*0.90)}, "label": "SST Block 2"}},
            {{"type": "cooldown", "duration_sec": 600, "power_low": {round(ftp*0.55)}, "power_high": {round(ftp*0.40)}, "label": "Cool-down"}}
          ]
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
          "rest": true,
          "intervals": []
        }}
      ]
    }}
  ]
}}

Intervall-Typen: warmup (power_low+power_high), work (power), rest (power), cooldown (power_low+power_high)
Alle Power-Werte als absolute Watt (nicht Prozent).
Variiere die Einheiten: Z2 Grundlage, SST, Schwellenintervalle, VO2max.
Antworte NUR mit dem JSON, kein Text davor oder danach."""

        res = req.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 8000, "messages": [{"role": "user", "content": prompt}]},
            timeout=90
        )
        data = res.json()
        text = "".join(b.get("text", "") for b in data.get("content", []))

        # Parse JSON — robust
        text_clean = text.strip()
        if text_clean.startswith("```"):
            text_clean = text_clean.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        # Falls JSON abgeschnitten — versuche zu reparieren
        try:
            plan_data = json.loads(text_clean)
        except json.JSONDecodeError as e:
            print(f"JSON parse error: {e}")
            print(f"Raw text length: {len(text_clean)}")
            # Versuche abgeschnittenes JSON zu reparieren
            # Finde letztes vollständiges Objekt
            pos = len(text_clean)
            for close in [']}]}', ']}', '}}']:
                idx = text_clean.rfind(close)
                if idx > 0:
                    candidate = text_clean[:idx+len(close)]
                    try:
                        plan_data = json.loads(candidate)
                        print(f"JSON repaired at position {idx}")
                        break
                    except:
                        continue
            else:
                return jsonify({"ok": False, "error": f"JSON Parse Fehler: {str(e)}\n\nTipp: Weniger Wochen wählen (2 statt 4) oder erneut versuchen."}), 500

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


@app.route("/download-zwo-get", methods=["GET"])
def download_zwo_get():
    """GET-Version des ZWO Downloads — funktioniert direkt im Browser."""
    workout_date = request.args.get("date")
    ftp = int(request.args.get("ftp", 210))

    try:
        with get_db() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT plan FROM training_plan ORDER BY generated_at DESC LIMIT 1")
                row = cur.fetchone()

        if not row:
            return "Kein Plan gefunden", 404

        plan = row["plan"] if isinstance(row["plan"], dict) else json.loads(row["plan"])

        workout = None
        for week in plan.get("weeks", []):
            for day in week.get("days", []):
                if day.get("date") == workout_date:
                    workout = day
                    break

        if not workout or workout.get("rest"):
            return "Kein Training für dieses Datum", 404

        intervals = workout.get("intervals", [])
        if not intervals:
            return "Keine Intervall-Daten — Plan neu generieren", 404

        title = workout.get("title", "Workout").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        desc = workout.get("description", "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        xml_parts = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<workout_file>',
            f'  <author>Cycling Coach AI</author>',
            f'  <n>{title}</n>',
            f'  <description>{desc}</description>',
            f'  <sportType>bike</sportType>',
            f'  <tags><tag name="AI Coach"/></tags>',
            '  <workout>',
        ]

        for iv in intervals:
            iv_type = iv.get("type", "work")
            dur = iv.get("duration_sec", 300)
            label = iv.get("label", "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

            if iv_type == "warmup":
                p_low = round(iv.get("power_low", ftp * 0.45) / ftp, 3)
                p_high = round(iv.get("power_high", ftp * 0.65) / ftp, 3)
                xml_parts.append(f'    <Warmup Duration="{dur}" PowerLow="{p_low}" PowerHigh="{p_high}"><textevent timeoffset="0" message="{label}"/></Warmup>')
            elif iv_type == "cooldown":
                p_low = round(iv.get("power_low", ftp * 0.40) / ftp, 3)
                p_high = round(iv.get("power_high", ftp * 0.55) / ftp, 3)
                xml_parts.append(f'    <Cooldown Duration="{dur}" PowerLow="{min(p_low,p_high)}" PowerHigh="{max(p_low,p_high)}"><textevent timeoffset="0" message="{label}"/></Cooldown>')
            else:
                power = round(iv.get("power", ftp * 0.75) / ftp, 3)
                xml_parts.append(f'    <SteadyState Duration="{dur}" Power="{power}"><textevent timeoffset="0" message="{label}"/></SteadyState>')

        xml_parts += ['  </workout>', '</workout_file>']
        zwo_content = "\n".join(xml_parts)
        filename = f"{workout_date}_{title.replace(' ', '_')[:30]}.zwo"

        return Response(
            zwo_content,
            mimetype="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )
    except Exception as e:
        return str(e), 500


@app.route("/download-zwo", methods=["POST"])
def download_zwo():
    """Generiert eine .zwo Zwift Workout Datei für eine Trainingseinheit."""
    body = request.get_json() or {}
    workout_date = body.get("date")
    ftp = body.get("ftp", 210)

    try:
        # Plan holen
        with get_db() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT plan FROM training_plan ORDER BY generated_at DESC LIMIT 1")
                row = cur.fetchone()

        if not row:
            return jsonify({"ok": False, "error": "Kein Plan gefunden"}), 404

        plan = row["plan"] if isinstance(row["plan"], dict) else json.loads(row["plan"])

        # Workout für dieses Datum finden
        workout = None
        for week in plan.get("weeks", []):
            for day in week.get("days", []):
                if day.get("date") == workout_date:
                    workout = day
                    break

        if not workout or workout.get("rest"):
            return jsonify({"ok": False, "error": "Kein Training für dieses Datum"}), 404

        intervals = workout.get("intervals", [])
        if not intervals:
            return jsonify({"ok": False, "error": "Keine Intervall-Daten vorhanden — Plan neu generieren"}), 404

        # ZWO XML generieren
        title = workout.get("title", "Cycling Workout").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        desc = workout.get("description", "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        xml_parts = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<workout_file>',
            f'  <author>Cycling Coach AI</author>',
            f'  <name>{title}</name>',
            f'  <description>{desc}</description>',
            f'  <sportType>bike</sportType>',
            f'  <tags><tag name="AI Coach"/></tags>',
            f'  <workout>',
        ]

        for iv in intervals:
            iv_type = iv.get("type", "work")
            dur = iv.get("duration_sec", 300)
            label = iv.get("label", "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

            if iv_type == "warmup":
                p_low = round(iv.get("power_low", ftp * 0.45) / ftp, 3)
                p_high = round(iv.get("power_high", ftp * 0.65) / ftp, 3)
                xml_parts.append(f'    <Warmup Duration="{dur}" PowerLow="{p_low}" PowerHigh="{p_high}"><textevent timeoffset="0" message="{label}"/></Warmup>')

            elif iv_type == "cooldown":
                p_low = round(iv.get("power_low", ftp * 0.55) / ftp, 3)
                p_high = round(iv.get("power_high", ftp * 0.40) / ftp, 3)
                # ZWO cooldown goes from high to low
                xml_parts.append(f'    <Cooldown Duration="{dur}" PowerLow="{min(p_low,p_high)}" PowerHigh="{max(p_low,p_high)}"><textevent timeoffset="0" message="{label}"/></Cooldown>')

            elif iv_type in ("work", "rest"):
                power = round(iv.get("power", ftp * 0.75) / ftp, 3)
                xml_parts.append(f'    <SteadyState Duration="{dur}" Power="{power}"><textevent timeoffset="0" message="{label}"/></SteadyState>')

        xml_parts += ['  </workout>', '</workout_file>']
        zwo_content = "\n".join(xml_parts)

        filename = f"{workout_date}_{title.replace(' ', '_')[:30]}.zwo"
        return Response(
            zwo_content,
            mimetype="application/xml",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": "application/xml"
            }
        )
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

# Auto-init on import (für gunicorn)
try:
    init_db()
except Exception as e:
    print(f"Auto DB init: {e}")
