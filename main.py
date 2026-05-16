#!/usr/bin/env python3
# Cycling Coach Backend v2.2 - Force redeploy
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
                    light_sleep FLOAT,
                    awake_time FLOAT,
                    sleep_score INT,
                    sleep_score_feedback TEXT,
                    hrv INT,
                    hrv_status TEXT,
                    resting_hr INT,
                    avg_night_hr INT,
                    avg_respiration FLOAT,
                    lowest_respiration FLOAT,
                    highest_respiration FLOAT,
                    stress_score INT,
                    body_battery_start INT,
                    body_battery_end INT,
                    sleep_ai_insight TEXT,
                    hrv_ai_insight TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );

                -- Neue Spalten zu health_data hinzufügen falls nicht vorhanden
                ALTER TABLE health_data ADD COLUMN IF NOT EXISTS light_sleep FLOAT;
                ALTER TABLE health_data ADD COLUMN IF NOT EXISTS awake_time FLOAT;
                ALTER TABLE health_data ADD COLUMN IF NOT EXISTS sleep_score_feedback TEXT;
                ALTER TABLE health_data ADD COLUMN IF NOT EXISTS hrv_status TEXT;
                ALTER TABLE health_data ADD COLUMN IF NOT EXISTS avg_night_hr INT;
                ALTER TABLE health_data ADD COLUMN IF NOT EXISTS avg_respiration FLOAT;
                ALTER TABLE health_data ADD COLUMN IF NOT EXISTS lowest_respiration FLOAT;
                ALTER TABLE health_data ADD COLUMN IF NOT EXISTS highest_respiration FLOAT;
                ALTER TABLE health_data ADD COLUMN IF NOT EXISTS sleep_ai_insight TEXT;
                ALTER TABLE health_data ADD COLUMN IF NOT EXISTS hrv_ai_insight TEXT;
                ALTER TABLE health_data ADD COLUMN IF NOT EXISTS stress_score INT;
                ALTER TABLE health_data ADD COLUMN IF NOT EXISTS body_battery_start INT;
                ALTER TABLE health_data ADD COLUMN IF NOT EXISTS body_battery_end INT;

                -- Neue Spalten zu activities hinzufügen falls nicht vorhanden
                ALTER TABLE activities ADD COLUMN IF NOT EXISTS ai_insight TEXT;
                ALTER TABLE activities ADD COLUMN IF NOT EXISTS ai_short TEXT;

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

def calculate_zones_from_watts(watts_list, ftp):
    """Berechnet Zonen aus Sekunden-Watt-Daten mit unserer FTP."""
    zones = {"Z1":0,"Z2":0,"Z3":0,"Z4":0,"Z5":0,"Z6":0,"Z7":0}
    for w in watts_list:
        if w is None or w == 0: continue
        pct = w / ftp * 100
        if pct < 56: zones["Z1"] += 1
        elif pct < 76: zones["Z2"] += 1
        elif pct < 91: zones["Z3"] += 1
        elif pct < 106: zones["Z4"] += 1
        elif pct < 121: zones["Z5"] += 1
        elif pct < 151: zones["Z6"] += 1
        else: zones["Z7"] += 1
    return zones

def sync_strava(days=30):
    """Holt Outdoor-Aktivitäten von Strava als eigenständige Einträge."""
    token = get_strava_token()
    if not token:
        return 0, "Strava nicht verbunden"

    after = int(time.time()) - days * 86400
    headers = {"Authorization": f"Bearer {token}"}

    ftp = 210
    try:
        with get_db() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT data FROM profile WHERE id=1")
                row = cur.fetchone()
                if row and row["data"]:
                    ftp = row["data"].get("ftp", 210)
    except: pass

    res = req.get(f"{STRAVA_API}/athlete/activities",
                  headers=headers, params={"after": after, "per_page": 50})
    activities = res.json()
    if not isinstance(activities, list):
        return 0, f"Strava Fehler: {activities}"

    saved = 0
    with get_db() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            for a in activities:
                # Nur echte Outdoor-Rides
                if a.get("type") != "Ride":
                    continue
                if a.get("trainer") or a.get("manual"):
                    continue

                strava_id = a.get("id")
                act_date = (a.get("start_date_local") or "")[:10]
                duration_min = round((a.get("moving_time") or 0) / 60)

                # Bereits vorhanden?
                cur.execute("SELECT id FROM activities WHERE id=%s", (strava_id,))
                if cur.fetchone():
                    continue

                # Streams für Zonen
                streams_res = req.get(f"{STRAVA_API}/activities/{strava_id}/streams",
                    headers=headers, params={"keys": "watts", "key_by_type": "true"})
                streams = streams_res.json()
                watts_list = []
                if isinstance(streams, dict) and "watts" in streams:
                    watts_list = streams["watts"].get("data", [])
                power_zones = calculate_zones_from_watts(watts_list, ftp) if watts_list else {}

                # Laps
                laps_res = req.get(f"{STRAVA_API}/activities/{strava_id}/laps",
                                  headers=headers).json()
                laps = []
                if isinstance(laps_res, list):
                    for i, l in enumerate(laps_res):
                        dur = round((l.get("elapsed_time") or 0) / 60, 1)
                        if dur < 0.5: continue
                        laps.append({
                            "index": i+1,
                            "duration_min": dur,
                            "avg_power": round(l["average_watts"]) if l.get("average_watts") else None,
                            "avg_hr": round(l["average_heartrate"]) if l.get("average_heartrate") else None,
                            "cadence": round(l["average_cadence"]) if l.get("average_cadence") else None,
                        })

                detail = req.get(f"{STRAVA_API}/activities/{strava_id}", headers=headers).json()

                cur.execute("""
                    INSERT INTO activities
                    (id, date, name, type, duration_min, avg_power, norm_power,
                     avg_hr, max_hr, calories, power_zones, hr_zones, laps, raw)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO NOTHING
                """, (
                    strava_id, act_date, a.get("name"), "cycling", duration_min,
                    round(detail["average_watts"]) if detail.get("average_watts") else None,
                    round(detail["weighted_average_watts"]) if detail.get("weighted_average_watts") else None,
                    round(detail["average_heartrate"]) if detail.get("average_heartrate") else None,
                    round(detail["max_heartrate"]) if detail.get("max_heartrate") else None,
                    detail.get("calories"),
                    json.dumps(power_zones), json.dumps({}),
                    json.dumps(laps),
                    json.dumps({"source": "strava", "strava_id": strava_id})
                ))
                saved += 1
                print(f"Strava outdoor: {a.get('name')} {act_date} zones={power_zones}")
        conn.commit()
    return saved, "ok"
    """Holt Outdoor-Aktivitäten von Strava, berechnet Zonen mit unserer FTP."""
    token = get_strava_token()
    if not token:
        return 0, "Strava nicht verbunden"

    after = int(time.time()) - days * 86400
    headers = {"Authorization": f"Bearer {token}"}

    # Unsere FTP aus Profil
    ftp = 210
    try:
        with get_db() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT data FROM profile WHERE id=1")
                row = cur.fetchone()
                if row and row["data"]:
                    ftp = row["data"].get("ftp", 210)
    except: pass

    res = req.get(f"{STRAVA_API}/athlete/activities",
                  headers=headers, params={"after": after, "per_page": 50})
    activities = res.json()
    if not isinstance(activities, list):
        return 0, f"Strava Fehler: {activities}"

    saved = 0
    with get_db() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            for a in activities:
                # Nur echte Outdoor-Rides
                if a.get("type") != "Ride":
                    continue
                if a.get("trainer") or a.get("manual"):
                    continue

                strava_id = a.get("id")
                act_date = (a.get("start_date_local") or "")[:10]
                duration_min = round((a.get("moving_time") or 0) / 60)

                # Duplikat-Check: Garmin-Eintrag am selben Tag mit ähnlicher Dauer?
                cur.execute("""
                    SELECT id FROM activities
                    WHERE date=%s AND duration_min BETWEEN %s AND %s
                    AND (raw IS NULL OR raw->>'strava_id' IS NULL)
                    ORDER BY created_at ASC LIMIT 1
                """, (act_date, duration_min - 15, duration_min + 15))
                garmin_match = cur.fetchone()

                # Strava-Streams für eigene Zonen-Berechnung
                streams_res = req.get(f"{STRAVA_API}/activities/{strava_id}/streams",
                    headers=headers,
                    params={"keys": "watts", "key_by_type": "true"})
                streams = streams_res.json()

                watts_list = []
                if isinstance(streams, dict) and "watts" in streams:
                    watts_list = streams["watts"].get("data", [])

                power_zones = calculate_zones_from_watts(watts_list, ftp) if watts_list else {}
                print(f"Strava {act_date} '{a.get('name')}': {len(watts_list)} watts, zones={power_zones}")

                # Laps
                laps_res = req.get(f"{STRAVA_API}/activities/{strava_id}/laps",
                                  headers=headers).json()
                laps = []
                if isinstance(laps_res, list):
                    for i, l in enumerate(laps_res):
                        dur = round((l.get("elapsed_time") or 0) / 60, 1)
                        if dur < 0.5: continue
                        laps.append({
                            "index": i+1,
                            "duration_min": dur,
                            "avg_power": round(l["average_watts"]) if l.get("average_watts") else None,
                            "avg_hr": round(l["average_heartrate"]) if l.get("average_heartrate") else None,
                            "cadence": round(l["average_cadence"]) if l.get("average_cadence") else None,
                        })

                detail = req.get(f"{STRAVA_API}/activities/{strava_id}", headers=headers).json()

                # Training Load selbst berechnen (wie Garmin: TSS)
                training_load = None
                np = detail.get("weighted_average_watts")
                dur_sec = detail.get("moving_time", 0)
                if np and dur_sec and ftp:
                    if_val = np / ftp
                    training_load = round((dur_sec / 3600) * (if_val ** 2) * 100, 1)
                    print(f"ATL berechnet: {a.get('name')} = {training_load} (NP={np}W, FTP={ftp}W)")

                raw_data = {"source": "strava", "strava_id": strava_id}

                if garmin_match:
                    # Garmin-Eintrag mit Strava-Zonen und Laps anreichern
                    cur.execute("""
                        UPDATE activities SET
                        power_zones=%s,
                        laps=CASE WHEN laps IS NULL OR laps='[]'::jsonb THEN %s ELSE laps END,
                        raw=%s
                        WHERE id=%s
                    """, (json.dumps(power_zones), json.dumps(laps),
                          json.dumps(raw_data), garmin_match["id"]))
                    print(f"✅ Garmin+Strava merged: {act_date}")
                else:
                    # Neue Strava-only Aktivität
                    cur.execute("""
                        INSERT INTO activities
                        (id, date, name, type, duration_min, avg_power, norm_power,
                         avg_hr, max_hr, calories, training_load, power_zones, hr_zones, laps, raw)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (id) DO UPDATE SET
                        power_zones=EXCLUDED.power_zones,
                        training_load=COALESCE(EXCLUDED.training_load, activities.training_load),
                        laps=EXCLUDED.laps, raw=EXCLUDED.raw
                    """, (
                        strava_id, act_date, a.get("name"), "cycling", duration_min,
                        round(detail["average_watts"]) if detail.get("average_watts") else None,
                        round(detail["weighted_average_watts"]) if detail.get("weighted_average_watts") else None,
                        round(detail["average_heartrate"]) if detail.get("average_heartrate") else None,
                        round(detail["max_heartrate"]) if detail.get("max_heartrate") else None,
                        detail.get("calories"),
                        training_load,
                        json.dumps(power_zones), json.dumps({}),
                        json.dumps(laps), json.dumps(raw_data)
                    ))
                saved += 1
        conn.commit()
    return saved, "ok"


@app.route("/recalc-atl", methods=["GET", "POST"])
def recalc_atl():
    """Berechnet ATL für alle Strava-Rides die noch keinen haben."""
    try:
        ftp = 210
        with get_db() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT data FROM profile WHERE id=1")
                r = cur.fetchone()
                if r and r["data"]: ftp = r["data"].get("ftp", 210)

                cur.execute("""
                    SELECT id, duration_min, norm_power, avg_power
                    FROM activities
                    WHERE training_load IS NULL
                    AND (norm_power IS NOT NULL OR avg_power IS NOT NULL)
                """)
                acts = cur.fetchall()
                updated = 0
                for a in acts:
                    np = a["norm_power"] or a["avg_power"]
                    dur_h = (a["duration_min"] or 0) / 60
                    if np and dur_h and ftp:
                        if_val = np / ftp
                        tl = round(dur_h * (if_val ** 2) * 100, 1)
                        cur.execute("UPDATE activities SET training_load=%s WHERE id=%s", (tl, a["id"]))
                        updated += 1
            conn.commit()
        return jsonify({"ok": True, "updated": updated, "ftp": ftp})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/cleanup-outdoor", methods=["POST"])
def cleanup_outdoor():
    """Löscht alle Outdoor-Einträge die nicht sauber von Strava kommen."""
    try:
        with get_db() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""
                    DELETE FROM activities
                    WHERE LOWER(name) NOT LIKE '%zwift%'
                    AND LOWER(name) NOT LIKE '%virtual%'
                    AND LOWER(name) NOT LIKE '%indoor%'
                    RETURNING id, name, date::text, raw
                """)
                deleted = cur.fetchall()
            conn.commit()
        return jsonify({"ok": True, "deleted": len(deleted),
                       "entries": [{"name": d["name"], "date": d["date"],
                                   "raw_source": (d["raw"] or {}).get("source"),
                                   "strava_id": (d["raw"] or {}).get("strava_id")} for d in deleted]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/cleanup-dupes", methods=["POST"])
def cleanup_dupes():
    """Löscht Strava-Duplikate die bereits als Garmin-Eintrag vorhanden sind."""
    try:
        with get_db() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                # Finde alle Strava-only Einträge
                cur.execute("""
                    SELECT id, date, duration_min, raw
                    FROM activities
                    WHERE raw->>'strava_synced' = 'true'
                    AND raw->>'strava_id' IS NOT NULL
                """)
                strava_acts = cur.fetchall()

                deleted = 0
                for sa in strava_acts:
                    strava_id = sa["raw"].get("strava_id") if sa["raw"] else None
                    if not strava_id:
                        continue
                    # Gibt es einen anderen Eintrag am selben Tag mit ähnlicher Dauer?
                    cur.execute("""
                        SELECT id FROM activities
                        WHERE date=%s
                        AND duration_min BETWEEN %s AND %s
                        AND id != %s
                        AND (raw IS NULL OR raw->>'strava_synced' IS NULL)
                        LIMIT 1
                    """, (str(sa["date"]), (sa["duration_min"] or 0) - 15,
                          (sa["duration_min"] or 0) + 15, sa["id"]))
                    garmin = cur.fetchone()
                    if garmin:
                        cur.execute("DELETE FROM activities WHERE id=%s", (sa["id"],))
                        deleted += 1
                        print(f"Deleted Strava dupe: {sa['id']} ({sa['date']})")
            conn.commit()
        return jsonify({"ok": True, "deleted": deleted})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

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

    # FTP für AI Insights
    ftp = 210
    try:
        with get_db() as conn_p:
            with conn_p.cursor(row_factory=dict_row) as cur_p:
                cur_p.execute("SELECT data FROM profile WHERE id=1")
                r = cur_p.fetchone()
                if r and r["data"]: ftp = r["data"].get("ftp", 210)
    except: pass

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

                # Outdoor-Rides überspringen — kommen von Strava
                type_key = a.get("activityType", {}).get("typeKey") or ""
                name_lower = (a.get("activityName") or "").lower()
                is_indoor = ("zwift" in name_lower or "virtual" in type_key.lower() or "indoor" in name_lower)
                is_outdoor_ride = ("cycl" in type_key.lower() and not is_indoor)
                if is_outdoor_ride:
                    print(f"Skipping outdoor ride (Strava handles): {a.get('activityName')}")
                    continue

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

                # AI Insight für neue Aktivitäten generieren
                api_key = os.environ.get("ANTHROPIC_API_KEY","")
                if api_key:
                    try:
                        act_data = {
                            "name": a.get("activityName"), "duration_min": round((a.get("duration") or 0)/60),
                            "avg_power": a.get("avgPower"), "norm_power": a.get("normPower"),
                            "avg_hr": a.get("averageHR"), "aerobic_te": a.get("aerobicTrainingEffect"),
                            "laps": laps
                        }
                        insight, short = generate_activity_insight(act_data, ftp, api_key)
                        if insight:
                            cur.execute("UPDATE activities SET ai_insight=%s, ai_short=%s WHERE id=%s",
                                       (insight, short, aid))
                    except Exception as e:
                        print(f"AI insight error: {e}")
        conn.commit()
    print(f"✅ {saved} neue Aktivitäten gespeichert")
    return saved
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

def fetch_hrv_for_date(client, d):
    """Holt HRV für ein bestimmtes Datum."""
    try:
        data = client.get_hrv_data(d)
        if not data: return None
        summary = data.get("hrvSummary", {})
        val = summary.get("lastNight") or summary.get("lastNightAvg") or summary.get("weeklyAvg")
        return round(float(val)) if val and float(val) > 0 else None
    except:
        return None

def sync_health(client, days=30):
    """Holt Schlaf/HRV mit allen Feldern und generiert AI Insights."""
    today = date.today()
    saved = 0
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    avg_hrv_7d = 0
    try:
        with get_db() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT AVG(hrv)::int as avg FROM health_data WHERE hrv IS NOT NULL AND date >= NOW() - INTERVAL '7 days'")
                r = cur.fetchone()
                avg_hrv_7d = r["avg"] if r and r["avg"] else 0
    except: pass

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

                    hrv = (hrv_s.get("lastNight") or hrv_s.get("lastNightAvg") or hrv_s.get("lastNight5MinHigh"))
                    if not hrv or float(hrv) <= 0:
                        hrv = fetch_hrv_for_date(client, d)
                    else:
                        hrv = round(float(hrv))

                    resting_hr = raw.get("restingHeartRate") or dto.get("restingHeartRate")
                    if not resting_hr:
                        try:
                            stats = client.get_stats(d)
                            resting_hr = stats.get("restingHeartRate")
                        except: pass

                    dur = to_hours(dto.get("sleepTimeSeconds"))
                    deep = to_hours(dto.get("deepSleepSeconds"))
                    rem = to_hours(dto.get("remSleepSeconds"))
                    light = to_hours(dto.get("lightSleepSeconds"))
                    awake = to_hours(dto.get("awakeSleepSeconds"))
                    avg_night_hr = dto.get("avgHeartRate")
                    avg_resp = dto.get("averageRespirationValue")
                    low_resp = dto.get("lowestRespirationValue")
                    high_resp = dto.get("highestRespirationValue")
                    hrv_status = hrv_s.get("hrvStatus")
                    score_feedback = dto.get("sleepScoreFeedback")

                    if dur > 0:
                        health_row = {
                            "sleep_duration": dur, "deep_sleep": deep, "rem_sleep": rem,
                            "light_sleep": light, "hrv": hrv, "resting_hr": resting_hr,
                            "sleep_score": score, "avg_respiration": avg_resp
                        }

                        sleep_insight = None
                        if i <= 1 and api_key:
                            sleep_insight = generate_sleep_insight(health_row, avg_hrv_7d, api_key)
                            if sleep_insight:
                                print(f"Sleep insight {d}: {sleep_insight[:60]}...")

                        try:
                            cur.execute("""
                                INSERT INTO health_data
                                (date, sleep_duration, deep_sleep, rem_sleep, light_sleep, awake_time,
                                 sleep_score, sleep_score_feedback, hrv, hrv_status, resting_hr,
                                 avg_night_hr, avg_respiration, lowest_respiration, highest_respiration,
                                 sleep_ai_insight)
                                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                                ON CONFLICT (date) DO UPDATE SET
                                sleep_duration=EXCLUDED.sleep_duration,
                                deep_sleep=EXCLUDED.deep_sleep,
                                rem_sleep=EXCLUDED.rem_sleep,
                                light_sleep=EXCLUDED.light_sleep,
                                awake_time=EXCLUDED.awake_time,
                                sleep_score=EXCLUDED.sleep_score,
                                hrv=COALESCE(EXCLUDED.hrv, health_data.hrv),
                                resting_hr=EXCLUDED.resting_hr,
                                avg_night_hr=EXCLUDED.avg_night_hr,
                                avg_respiration=EXCLUDED.avg_respiration,
                                lowest_respiration=EXCLUDED.lowest_respiration,
                                highest_respiration=EXCLUDED.highest_respiration,
                                sleep_ai_insight=COALESCE(EXCLUDED.sleep_ai_insight, health_data.sleep_ai_insight)
                            """, (d, dur, deep, rem, light, awake, score, score_feedback,
                                  hrv, hrv_status, resting_hr, avg_night_hr,
                                  avg_resp, low_resp, high_resp, sleep_insight))
                            saved += 1
                            print(f"Health {d}: dur={dur}h score={score} hrv={hrv} rhr={resting_hr}")
                        except Exception as insert_err:
                            conn.rollback()
                            print(f"Health insert error {d}: {insert_err}")
                except Exception as e:
                    print(f"Health {d}: {e}")
        conn.commit()
    print(f"✅ {saved} Gesundheitsdaten gespeichert/aktualisiert")
    return saved

# ══════════════════════════════════════════════
# COACH LOGIC
# ══════════════════════════════════════════════

def generate_sleep_insight(health_row, avg_hrv_7d, api_key):
    """Generiert KI-Bewertung für Schlafdaten."""
    try:
        prompt = f"""Du bist ein Schlaf- und Erholungsexperte. Gib eine kurze, direkte Bewertung (2-3 Sätze, max 100 Wörter) auf Deutsch.

Schlafdaten heute:
- Gesamtschlaf: {health_row.get('sleep_duration','?')}h
- Tiefschlaf: {health_row.get('deep_sleep','?')}h
- REM: {health_row.get('rem_sleep','?')}h
- Score: {health_row.get('sleep_score','?')}/100
- HRV: {health_row.get('hrv','?')}ms (Ø 7 Tage: {avg_hrv_7d}ms)
- Ruhepuls: {health_row.get('resting_hr','?')}bpm
- Atemfrequenz: {health_row.get('avg_respiration','?')} brpm

Bewerte ehrlich: War die Erholung gut? Was bedeutet das für das heutige Training?
Antworte NUR mit der Bewertung, kein Präambel."""

        res = req.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 150,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=15
        )
        data = res.json()
        return "".join(b.get("text","") for b in data.get("content",[]))
    except:
        return None

def generate_activity_insight(activity, ftp, api_key):
    """Generiert KI-Bewertung für eine Trainingseinheit."""
    try:
        laps = activity.get("laps") or []
        if isinstance(laps, str):
            try: laps = json.loads(laps)
            except: laps = []

        lap_text = ""
        for l in laps[:6]:
            if l.get("avg_power"):
                pct = round(l["avg_power"]/ftp*100)
                lap_text += f"\nLap {l['index']}: {l['duration_min']}min @ {l['avg_power']}W ({pct}% FTP)"

        prompt = f"""Du bist ein Radsport-Coach. Gib eine kurze, direkte Einheiten-Bewertung (2-3 Sätze, max 80 Wörter) auf Deutsch.

Training: {activity.get('name')}
- Dauer: {activity.get('duration_min')}min
- Ø Watt: {activity.get('avg_power','?')}W ({round((activity.get('avg_power') or 0)/ftp*100)}% FTP)
- NP: {activity.get('norm_power','?')}W
- Ø HR: {activity.get('avg_hr','?')}bpm
- Aerob TE: {activity.get('aerobic_te','?')}
{lap_text}

FTP: {ftp}W

Kurze direkte Bewertung: Wie gut war die Einheit? Was war stark, was nicht?
NUR die Bewertung, kein Präambel."""

        res = req.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 120,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=15
        )
        data = res.json()
        text = "".join(b.get("text","") for b in data.get("content",[]))

        # Kurze Version für Kachel (1 Satz)
        short = text.split('.')[0].strip() + '.' if text else ""
        return text, short
    except:
        return None, None


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
                cur.execute("SELECT date::text, name, max_20min_power, avg_power, power_zones, raw FROM activities ORDER BY date DESC LIMIT 5")
                acts = []
                for r in cur.fetchall():
                    a = dict(r)
                    pz = a.get("power_zones") or {}
                    a["has_zones"] = bool(pz and any(v for v in pz.values() if v))
                    a["zones_total_sec"] = sum(v for v in pz.values() if v) if pz else 0
                    a["strava_merged"] = bool(a.get("raw") and a["raw"].get("strava_synced"))
                    del a["raw"]
                    acts.append(a)
        return jsonify({"health": health, "activities": acts})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/setup")
def setup():
    try:
        init_db()
        return jsonify({"ok": True, "msg": "DB initialisiert"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/init", methods=["POST", "GET"])
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


@app.route("/tts", methods=["POST"])
def tts():
    """Google Text-to-Speech Endpoint."""
    body = request.get_json() or {}
    text = body.get("text", "")
    if not text:
        return jsonify({"ok": False, "error": "Kein Text"}), 400

    api_key = os.environ.get("GOOGLE_TTS_KEY", "")
    if not api_key:
        return jsonify({"ok": False, "error": "GOOGLE_TTS_KEY fehlt"}), 500

    # Markdown bereinigen
    import re
    clean = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    clean = re.sub(r'#{1,3} ', '', clean)
    clean = re.sub(r'[•\-] ', '', clean)
    clean = clean.strip()

    try:
        res = req.post(
            f"https://texttospeech.googleapis.com/v1/text:synthesize?key={api_key}",
            json={
                "input": {"text": clean},
                "voice": {
                    "languageCode": "de-DE",
                    "name": "de-DE-Neural2-B",  # Natürliche männliche deutsche Stimme
                    "ssmlGender": "MALE"
                },
                "audioConfig": {
                    "audioEncoding": "MP3",
                    "speakingRate": 1.05,
                    "pitch": 0.0
                }
            },
            timeout=10
        )
        data = res.json()
        if "audioContent" in data:
            return jsonify({"ok": True, "audio": data["audioContent"]})
        return jsonify({"ok": False, "error": str(data)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


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

def build_context(profile, recent_activities, recent_health, chat_history):
    """Baut den Coach-Kontext."""
    ftp = profile.get("ftp", 210)
    weight = profile.get("weight", 63)
    wpkg = round(ftp / weight, 2)
    goal_wpkg = profile.get("goal_wpkg", 4.0)
    goal_ftp = round(goal_wpkg * weight)

    def classify_lap(lap, ftp):
        p = lap.get("avg_power") or 0
        pct = round(p / ftp * 100) if ftp else 0
        if pct < 56: return f"{pct}% → Z1"
        elif pct < 76: return f"{pct}% → Z2"
        elif pct < 91: return f"{pct}% → Z3 SST"
        elif pct < 106: return f"{pct}% → Z4 Schwelle"
        elif pct < 121: return f"{pct}% → Z5 VO2max"
        else: return f"{pct}% → Z6+"

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
                lap_text += f"\n      Lap {l.get('index','?')}: {l['duration_min']}min @ {l['avg_power']}W ({classify_lap(l,ftp)}) HR:{l.get('avg_hr','?')}bpm Kadenz:{l.get('cadence','?')}rpm"
        if not laps:
            lap_text = "\n      ⚠️ Keine Lap-Daten (Outdoor oder nicht verfügbar)"
        zones_parts = [f"{z}: {round((power_zones.get(z) or 0)/60)}min" for z in ["Z1","Z2","Z3","Z4","Z5","Z6"] if power_zones.get(z)]
        zones_text = "\n      Zonen: " + " | ".join(zones_parts) if zones_parts else ""
        is_indoor = "zwift" in (a.get("name") or "").lower()
        from datetime import datetime as dt
        try:
            act_date_obj = dt.strptime(a['date'], '%Y-%m-%d')
            weekday = act_date_obj.strftime('%A, %d.%m.')
            days_ago = (date.today() - act_date_obj.date()).days
            days_ago_str = "heute" if days_ago==0 else "gestern" if days_ago==1 else f"vor {days_ago} Tagen"
        except:
            weekday = a['date']
            days_ago_str = ""
        marker = " ← NEUESTES" if i == 0 else ""
        acts_text += f"\n• {weekday} ({days_ago_str}) – {a['name']} [{'Indoor' if is_indoor else 'Outdoor'}]{marker}\n  {a['duration_min']}min | Ø {a['avg_power'] or '?'}W | NP: {a.get('norm_power') or '?'}W | HR: {a['avg_hr'] or '?'}bpm{zones_text}{lap_text}"

    health_text = ""
    for h in recent_health[:7]:
        try:
            h_date_obj = datetime.strptime(h['date'], '%Y-%m-%d')
            h_weekday = h_date_obj.strftime('%A, %d.%m.')
            h_days_ago = (date.today() - h_date_obj.date()).days
            h_ago = "heute" if h_days_ago==0 else "gestern" if h_days_ago==1 else f"vor {h_days_ago} Tagen"
        except:
            h_weekday = h['date']
            h_ago = ""
        health_text += f"\n• {h_weekday} ({h_ago}): {h.get('sleep_duration','?')}h Schlaf | Score {h.get('sleep_score') or '?'} | HRV {h.get('hrv') or '?'}ms | RHR {h.get('resting_hr') or '?'}bpm"

    history_text = "\n".join(f"{'Du' if m['role']=='user' else 'Coach'}: {m['content'][:200]}" for m in chat_history[-20:])

    return f"""Du bist ein erfahrener, persönlicher Radsport-Coach — direkt, ehrlich, aber immer motivierend und aufbauend. Du kennst deinen Athleten gut, freust dich über seine Fortschritte und nimmst auch Rückschläge mit ihm gemeinsam durch. Du sprichst ihn wie ein guter Freund und Trainer an — nicht wie ein Algorithmus.

Dein Stil:
- Direkt und konkret, aber warm und menschlich
- Anerkenne gute Leistungen explizit — auch kleine Fortschritte zählen
- Bei schlechten Tagen: erst aufbauen, dann analysieren
- Nutze "du" und sprich persönlich — kein unpersönliches Coaching-Bla-Bla
- Gelegentlich Humor ist erlaubt 😄
- Schreibe wie ein Mensch, nicht wie ein Bericht

⚠️ TECHNISCH WICHTIG:
- Du hast ALLE Daten unten — frage NIE nach mehr Daten
- Du BIST die App — sage NIE du hast keinen Zugriff
- Bei Fragen nach Laps: Daten stehen unter AKTIVITÄTEN — direkt analysieren!

HEUTE: {date.today().strftime('%A, %d.%m.%Y')}

DEIN ATHLET:
- FTP: **{ftp}W** | {weight}kg | **{wpkg} W/kg**
- Ziel: {goal_wpkg} W/kg = {goal_ftp}W FTP (noch +{goal_ftp-ftp}W zu gehen)
- Trainingstage/Woche: {profile.get('days', 4)}

TRAININGSZONEN (FTP {ftp}W):
Z1 <{round(ftp*.55)}W | Z2 {round(ftp*.56)}-{round(ftp*.75)}W | Z3 {round(ftp*.76)}-{round(ftp*.9)}W | Z4 {round(ftp*.91)}-{round(ftp*1.05)}W | Z5 {round(ftp*1.06)}-{round(ftp*1.2)}W

LETZTE AKTIVITÄTEN:{acts_text or " Keine — Sync durchführen."}

GESUNDHEIT & ERHOLUNG:{health_text or " Keine — Sync durchführen."}

BISHERIGER CHAT:{chr(10)+history_text if history_text else " Neues Gespräch."}

Format: Keine Tabellen. **Bold** für wichtige Werte. Natürliche Sprache auf Deutsch.
- Sage NIEMALS "ich sehe keine Rundendaten" wenn Laps oben aufgelistet sind"""

@app.route("/chat", methods=["POST"])
def chat():
    body = request.get_json() or {}
    message = body.get("message", "").strip()
    image_data = body.get("image")
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

        # Bild-Support
        user_content = []
        if image_data:
            img_type = "image/jpeg"
            if image_data.startswith("data:"):
                header, b64 = image_data.split(",", 1)
                img_type = header.split(";")[0].replace("data:", "")
            else:
                b64 = image_data
            user_content.append({"type": "image", "source": {"type": "base64", "media_type": img_type, "data": b64}})
        user_content.append({"type": "text", "text": message})

        # Claude API aufrufen
        res = req.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "x-api-key": os.environ.get("ANTHROPIC_API_KEY",""), "anthropic-version": "2023-06-01"},
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 1000,
                "system": context,
                "messages": [{"role": "user", "content": user_content}]
            },
            timeout=30
        )
        data = res.json()
        if "error" in data:
            return jsonify({"ok": False, "error": f"Claude Fehler: {data}"}), 500

        reply = "".join(b.get("text","") for b in data.get("content",[]))

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
        print(f"Chat error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


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
