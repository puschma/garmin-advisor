#!/usr/bin/env python3
"""
Garmin Training Advisor – Cloud Backend für Railway
"""

import json
import os
from datetime import date, timedelta
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

try:
    from garminconnect import Garmin
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "garminconnect"])
    from garminconnect import Garmin

app = Flask(__name__)
CORS(app)

# ─── In-memory Client Cache ───
_client_cache = {}

def get_client(email, password):
    """
    Authentifizierung in dieser Reihenfolge:
    1. RAM-Cache (schnell, kein Netzwerk)
    2. GARMIN_TOKEN Umgebungsvariable (kein Login nötig!)
    3. Frischer Login (nur als letzter Ausweg)
    """
    # 1. RAM-Cache
    if email in _client_cache:
        try:
            c = _client_cache[email]
            _ = c.display_name
            print("✅ RAM-Cache")
            return c
        except Exception:
            del _client_cache[email]

    # 2. Token aus Umgebungsvariable (bevorzugte Methode für Railway)
    env_token = os.environ.get("GARMIN_TOKEN")
    if env_token:
        try:
            c = Garmin(email, password)
            c.garth.loads(env_token)
            _ = c.display_name
            _client_cache[email] = c
            print("✅ ENV-Token")
            return c
        except Exception as e:
            print(f"⚠️ ENV-Token ungültig: {e}")

    # 3. Frischer Login (kann 429 auslösen bei zu vielen Versuchen)
    print("🔐 Frischer Login")
    c = Garmin(email, password)
    c.login()
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
    html = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Garmin Token Generator</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #07090f; color: #e8eaf0; font-family: system-ui, sans-serif;
    min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 24px; }
  .card { width: 100%; max-width: 420px; background: #0d1120;
    border: 1px solid rgba(255,255,255,0.08); border-radius: 20px; padding: 28px 24px; }
  h1 { font-size: 20px; font-weight: 700; margin-bottom: 6px; }
  .sub { font-size: 13px; color: rgba(232,234,240,0.45); margin-bottom: 24px; line-height: 1.5; }
  label { font-size: 11px; letter-spacing: 2px; text-transform: uppercase;
    color: rgba(232,234,240,0.4); display: block; margin-bottom: 7px; }
  input { width: 100%; padding: 13px 14px; margin-bottom: 14px;
    background: #121928; border: 1px solid rgba(255,255,255,0.07);
    border-radius: 12px; color: #e8eaf0; font-size: 15px; outline: none; }
  input:focus { border-color: #3b82f6; }
  button { width: 100%; padding: 15px; background: linear-gradient(135deg,#1d4ed8,#3b82f6);
    border: none; border-radius: 13px; color: #fff; font-size: 15px; font-weight: 700; cursor: pointer; }
  button:disabled { opacity: 0.45; cursor: not-allowed; }
  .result { margin-top: 20px; padding: 14px; background: #0a1628;
    border: 1px solid rgba(59,130,246,0.2); border-radius: 12px; display: none; }
  .result h3 { font-size: 13px; color: #4ade80; margin-bottom: 10px; }
  .token-box { background: #060a10; border: 1px solid rgba(255,255,255,0.06);
    border-radius: 8px; padding: 10px 12px; font-family: monospace; font-size: 10px;
    color: #93c5fd; word-break: break-all; max-height: 100px; overflow-y: auto;
    margin-bottom: 10px; line-height: 1.5; }
  .copy-btn { padding: 11px; background: rgba(74,222,128,0.12);
    border: 1px solid rgba(74,222,128,0.25); border-radius: 10px;
    color: #4ade80; font-size: 13px; font-weight: 600; cursor: pointer; margin-bottom: 12px; }
  .step { display: flex; gap: 10px; align-items: flex-start; margin-bottom: 7px; font-size: 12px; color: rgba(232,234,240,0.55); }
  .step-num { width: 20px; height: 20px; border-radius: 50%; background: rgba(59,130,246,0.2);
    color: #93c5fd; font-size: 11px; font-weight: 700; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center; }
  .err { margin-top: 14px; padding: 12px 14px; background: rgba(239,68,68,0.1);
    border: 1px solid rgba(239,68,68,0.2); border-radius: 10px;
    font-size: 13px; color: #fca5a5; display: none; line-height: 1.5; }
  .spinner { width: 16px; height: 16px; border-radius: 50%;
    border: 2px solid rgba(255,255,255,0.2); border-top-color: #fff;
    animation: spin 0.8s linear infinite; display: inline-block;
    margin-right: 8px; vertical-align: middle; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<div class="card">
  <h1>🔑 Garmin Token Generator</h1>
  <p class="sub">Einmalig einloggen &rarr; Token in Railway speichern.<br>Danach kein Login mehr nötig.</p>
  <label>Garmin E-Mail</label>
  <input id="email" type="email" placeholder="name@beispiel.de">
  <label>Garmin Passwort</label>
  <input id="pw" type="password" placeholder="••••••••">
  <button id="btn" onclick="go()">Token generieren</button>
  <div class="err" id="err"></div>
  <div class="result" id="result">
    <h3>✅ Token generiert!</h3>
    <div class="token-box" id="tokenBox"></div>
    <button class="copy-btn" onclick="copy()">📋 Token kopieren</button>
    <div class="step"><div class="step-num">1</div><span>Token kopieren (Button oben)</span></div>
    <div class="step"><div class="step-num">2</div><span>Railway Dashboard &rarr; dein Projekt &rarr; <b>Variables</b></span></div>
    <div class="step"><div class="step-num">3</div><span>New Variable: Name <b>GARMIN_TOKEN</b>, Value = Token</span></div>
    <div class="step"><div class="step-num">4</div><span>Speichern &rarr; Railway neu starten &rarr; fertig! 🎉</span></div>
  </div>
</div>
<script>
let tok = '';
async function go() {
  const email = document.getElementById('email').value.trim();
  const pw = document.getElementById('pw').value;
  document.getElementById('err').style.display = 'none';
  document.getElementById('result').style.display = 'none';
  if (!email || !pw) { showErr('Bitte E-Mail und Passwort eingeben.'); return; }
  const btn = document.getElementById('btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Verbinde mit Garmin…';
  try {
    const res = await fetch('/get-token', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({email, password: pw})
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error);
    tok = data.token;
    document.getElementById('tokenBox').textContent = tok;
    document.getElementById('result').style.display = 'block';
  } catch(e) { showErr(e.message); }
  btn.disabled = false; btn.textContent = 'Token generieren';
}
function copy() {
  navigator.clipboard.writeText(tok).catch(() => {
    const t = document.createElement('textarea');
    t.value = tok; document.body.appendChild(t); t.select();
    document.execCommand('copy'); document.body.removeChild(t);
  });
  const b = document.querySelector('.copy-btn');
  b.textContent = '✅ Kopiert!';
  setTimeout(() => b.textContent = '📋 Token kopieren', 2000);
}
function showErr(m) { const e = document.getElementById('err'); e.textContent = m; e.style.display = 'block'; }
</script>
</body>
</html>"""
    return Response(html, mimetype='text/html')

@app.route("/get-token", methods=["POST"])
def get_token():
    """Einmaliger Login → gibt den Token zurück zum Speichern als Env-Variable."""
    body = request.get_json() or {}
    email = body.get("email", "").strip()
    password = body.get("password", "")
    if not email or not password:
        return jsonify({"ok": False, "error": "E-Mail und Passwort fehlen"}), 400
    try:
        c = Garmin(email, password)
        c.login()
        token = c.garth.dumps()
        _client_cache[email] = c
        return jsonify({"ok": True, "token": token, "user": c.display_name})
    except Exception as e:
        err = str(e)
        if "429" in err or "Too Many Requests" in err:
            return jsonify({"ok": False, "error": "Garmin 429: Bitte 30 Minuten warten und erneut versuchen."}), 429
        return jsonify({"ok": False, "error": err}), 401

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
            return jsonify({
                "ok": False,
                "error": "Garmin 429: Bitte GARMIN_TOKEN als Railway-Umgebungsvariable setzen (siehe get_token.py)."
            }), 429
        return jsonify({"ok": False, "error": err}), 500

@app.route("/health")
def health():
    return jsonify({"ok": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
