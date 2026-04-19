#!/usr/bin/env python3
"""
Garmin Training Advisor – Cloud Backend für Railway
Authentifizierung via Browser-Cookies
"""

import json
import os
from datetime import date, timedelta
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import requests as req

app = Flask(__name__)
CORS(app)

GARMIN_API = "https://connect.garmin.com/proxy"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "NK": "NT",
    "Di-Backend": "connectapi.garmin.com",
    "X-app-ver": "4.84.0.0",
    "Accept": "application/json",
    "Origin": "https://connect.garmin.com",
    "Referer": "https://connect.garmin.com/modern/",
}

def get_session():
    token_str = os.environ.get("GARMIN_TOKEN", "")
    if not token_str:
        raise ValueError("GARMIN_TOKEN nicht gesetzt.")
    token = json.loads(token_str)
    session = req.Session()
    session.headers.update(HEADERS)
    for name, value in token.items():
        if name != "type" and value:
            session.cookies.set(name, value, domain=".garmin.com")
    return session

def get_user_id(session):
    """Holt Display-Name als User-ID."""
    urls = [
        f"{GARMIN_API}/userprofile-service/socialProfile",
        "https://connect.garmin.com/modern/currentuser-service/user/info",
        f"{GARMIN_API}/userprofile-service/profile/user-settings",
    ]
    for url in urls:
        try:
            r = session.get(url, timeout=10)
            if r.status_code == 200 and r.text not in ("{}", "null", ""):
                data = r.json()
                uid = (data.get("displayName") or data.get("userName")
                       or data.get("username") or data.get("userId"))
                if uid:
                    print(f"User ID: {uid} from {url}")
                    return str(uid)
        except Exception as e:
            print(f"get_user_id {url}: {e}")
    return None

def to_hours(secs):
    return round((secs or 0) / 3600, 1)

def fetch_sleep(session, days=7, user_id=None):
    results = []
    today = date.today()
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        try:
            urls = [f"{GARMIN_API}/wellness-service/wellness/dailySleepData/{d}"]
            if user_id:
                urls.insert(0, f"{GARMIN_API}/wellness-service/wellness/dailySleepData/{user_id}/{d}")

            raw = None
            for url in urls:
                r = session.get(url, params={"date": d, "nonSleepBufferMinutes": 60}, timeout=15)
                print(f"Sleep {d}: {r.status_code} {r.text[:80]}")
                if r.status_code == 200 and r.text.strip() not in ("{}", "null", "[]", ""):
                    raw = r.json()
                    break

            if not raw:
                continue

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

def fetch_training(session, days=7, user_id=None):
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

    activities = []
    urls = [
        f"{GARMIN_API}/activitylist-service/activities/search/activities",
    ]
    if user_id:
        urls.insert(0, f"{GARMIN_API}/activitylist-service/activities/{user_id}")

    for url in urls:
        try:
            r = session.get(url, params={"startDate": start, "endDate": end, "limit": 30, "start": 0}, timeout=15)
            print(f"Training: {r.status_code} {r.text[:80]}")
            if r.status_code == 200 and r.text.strip() not in ("{}", "null", "[]", ""):
                data = r.json()
                if isinstance(data, list) and len(data) > 0:
                    activities = data
                    break
                elif isinstance(data, dict) and data.get("activityList"):
                    activities = data["activityList"]
                    break
        except Exception as e:
            print(f"Training {url}: {e}")

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
    has_token = bool(os.environ.get("GARMIN_TOKEN"))
    status_cls = "ok" if has_token else "warn"
    status_msg = "✅ GARMIN_TOKEN gesetzt. Server einsatzbereit." if has_token else "⚠️ GARMIN_TOKEN fehlt."
    html = """<!DOCTYPE html>
<html lang="de"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Garmin Setup</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#07090f;color:#e8eaf0;font-family:system-ui,sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}
.card{width:100%;max-width:460px;background:#0d1120;border:1px solid rgba(255,255,255,0.08);border-radius:20px;padding:28px 24px}
h1{font-size:20px;font-weight:700;margin-bottom:6px}
.sub{font-size:13px;color:rgba(232,234,240,0.45);margin-bottom:20px;line-height:1.5}
.status{padding:12px 14px;border-radius:12px;font-size:13px;margin-bottom:20px}
.ok{background:rgba(74,222,128,0.1);border:1px solid rgba(74,222,128,0.2);color:#86efac}
.warn{background:rgba(251,146,60,0.1);border:1px solid rgba(251,146,60,0.2);color:#fdba74}
label{font-size:11px;letter-spacing:2px;text-transform:uppercase;color:rgba(232,234,240,0.4);display:block;margin-bottom:7px;margin-top:12px}
input{width:100%;padding:12px 14px;background:#121928;border:1px solid rgba(255,255,255,0.07);
  border-radius:12px;color:#e8eaf0;font-size:13px;outline:none;font-family:monospace}
input:focus{border-color:#3b82f6}
button{width:100%;padding:15px;background:linear-gradient(135deg,#1d4ed8,#3b82f6);
  border:none;border-radius:13px;color:#fff;font-size:15px;font-weight:700;cursor:pointer;margin-top:16px}
button:disabled{opacity:0.45;cursor:not-allowed}
.result{margin-top:20px;padding:14px;background:#0a1628;border:1px solid rgba(59,130,246,0.2);border-radius:12px;display:none}
.result h3{font-size:13px;color:#4ade80;margin-bottom:10px}
.token-box{background:#060a10;border:1px solid rgba(255,255,255,0.06);border-radius:8px;
  padding:10px;font-family:monospace;font-size:10px;color:#93c5fd;
  word-break:break-all;max-height:80px;overflow-y:auto;margin-bottom:10px;line-height:1.5}
.copy-btn{padding:11px;background:rgba(74,222,128,0.12);border:1px solid rgba(74,222,128,0.25);
  border-radius:10px;color:#4ade80;font-size:13px;font-weight:600;cursor:pointer;margin-bottom:12px;width:100%;margin-top:0}
.step{display:flex;gap:10px;align-items:flex-start;margin-bottom:7px;font-size:12px;color:rgba(232,234,240,0.55)}
.sn{width:20px;height:20px;border-radius:50%;background:rgba(59,130,246,0.2);color:#93c5fd;
  font-size:11px;font-weight:700;flex-shrink:0;display:flex;align-items:center;justify-content:center}
.err{margin-top:14px;padding:12px;background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.2);
  border-radius:10px;font-size:13px;color:#fca5a5;display:none;line-height:1.5}
.hint{font-size:12px;color:rgba(232,234,240,0.45);line-height:1.7;margin-bottom:4px}
.sec{font-size:11px;letter-spacing:2px;text-transform:uppercase;color:rgba(232,234,240,0.3);
  margin:20px 0 8px;padding-top:16px;border-top:1px solid rgba(255,255,255,0.06)}
a{color:#60a5fa} code{background:rgba(59,130,246,0.15);padding:1px 5px;border-radius:4px;font-family:monospace;font-size:12px;color:#93c5fd}
.sp{width:16px;height:16px;border-radius:50%;border:2px solid rgba(255,255,255,0.2);
  border-top-color:#fff;animation:spin .8s linear infinite;display:inline-block;margin-right:8px;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}
</style></head><body>
<div class="card">
  <h1>⌚ Garmin Advisor Setup</h1>
  <p class="sub">Cookies aus dem Browser kopieren → Token generieren → in Railway speichern.</p>
  <div class="status """ + status_cls + '">' + status_msg + """</div>
  <p class="hint">
    1. <a href="https://connect.garmin.com/modern/main" target="_blank">connect.garmin.com</a> öffnen &amp; einloggen<br>
    2. <code>F12</code> → Anwendung → Cookies → connect.garmin.com
  </p>
  <label>JWT_WEB *</label>
  <input id="c1" type="text" placeholder="eyJ..." autocapitalize="none" autocorrect="off" spellcheck="false">
  <label>GARMIN-SSO-cust-GUID *</label>
  <input id="c2" type="text" placeholder="xxxxxxxx-xxxx-..." autocapitalize="none" autocorrect="off" spellcheck="false">
  <label>SESSIONID</label>
  <input id="c3" type="text" placeholder="optional" autocapitalize="none" autocorrect="off" spellcheck="false">
  <label>GARMIN-SSO</label>
  <input id="c4" type="text" placeholder="optional" autocapitalize="none" autocorrect="off" spellcheck="false">
  <label>TASESSIONID</label>
  <input id="c5" type="text" placeholder="optional" autocapitalize="none" autocorrect="off" spellcheck="false">
  <button id="btn" onclick="go()">Token generieren &amp; validieren</button>
  <div class="err" id="err"></div>
  <div class="result" id="result">
    <h3>✅ Token bereit!</h3>
    <div class="token-box" id="tokenBox"></div>
    <button class="copy-btn" onclick="copy()">📋 Token kopieren</button>
    <div class="step"><div class="sn">1</div><span>Token kopieren</span></div>
    <div class="step"><div class="sn">2</div><span>Railway → Variables → <code>GARMIN_TOKEN</code> ersetzen</span></div>
    <div class="step"><div class="sn">3</div><span>Railway startet neu → App öffnen 🎉</span></div>
  </div>
</div>
<script>
let tok='';
async function go(){
  const cookies={type:'cookie',
    'JWT_WEB':document.getElementById('c1').value.trim(),
    'GARMIN-SSO-cust-GUID':document.getElementById('c2').value.trim(),
    'SESSIONID':document.getElementById('c3').value.trim(),
    'GARMIN-SSO':document.getElementById('c4').value.trim(),
    'TASESSIONID':document.getElementById('c5').value.trim(),
  };
  document.getElementById('err').style.display='none';
  document.getElementById('result').style.display='none';
  if(!cookies['JWT_WEB']||!cookies['GARMIN-SSO-cust-GUID']){showErr('JWT_WEB und GARMIN-SSO-cust-GUID sind Pflicht.');return;}
  Object.keys(cookies).forEach(k=>{if(!cookies[k])delete cookies[k];});
  const btn=document.getElementById('btn');
  btn.disabled=true;btn.innerHTML='<span class="sp"></span>Validiere…';
  try{
    const res=await fetch('/set-token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cookies})});
    const data=await res.json();
    if(!data.ok)throw new Error(data.error);
    tok=data.token;
    document.getElementById('tokenBox').textContent=tok;
    document.getElementById('result').style.display='block';
  }catch(e){showErr(e.message);}
  btn.disabled=false;btn.textContent='Token generieren & validieren';
}
function copy(){
  navigator.clipboard.writeText(tok).catch(()=>{
    const t=document.createElement('textarea');t.value=tok;
    document.body.appendChild(t);t.select();document.execCommand('copy');document.body.removeChild(t);
  });
  const b=document.querySelector('.copy-btn');
  b.textContent='✅ Kopiert!';setTimeout(()=>b.textContent='📋 Token kopieren',2000);
}
function showErr(m){const e=document.getElementById('err');e.textContent=m;e.style.display='block';}
</script></body></html>"""
    return Response(html, mimetype='text/html')


@app.route("/set-token", methods=["POST"])
def set_token():
    body = request.get_json() or {}
    cookies = body.get("cookies", {})
    if not cookies.get("JWT_WEB") or not cookies.get("GARMIN-SSO-cust-GUID"):
        return jsonify({"ok": False, "error": "JWT_WEB und GARMIN-SSO-cust-GUID sind Pflichtfelder"}), 400

    session = req.Session()
    session.headers.update(HEADERS)
    for name, value in cookies.items():
        if name != "type" and value:
            session.cookies.set(name, value, domain=".garmin.com")

    test_urls = [
        f"{GARMIN_API}/userprofile-service/socialProfile",
        f"{GARMIN_API}/usersummary-service/usersummary/daily/{date.today().isoformat()}",
        "https://connect.garmin.com/modern/currentuser-service/user/info",
    ]
    for url in test_urls:
        try:
            r = session.get(url, timeout=10)
            print(f"Validate {url}: {r.status_code} {r.text[:100]}")
            if r.status_code == 200:
                return jsonify({"ok": True, "token": json.dumps(cookies)})
            if r.status_code == 401:
                return jsonify({"ok": False, "error": "Cookies ungültig. Bitte neu einloggen."}), 401
        except Exception as e:
            print(f"Validate error: {e}")

    return jsonify({"ok": False, "error": "Garmin konnte nicht erreicht werden. Bitte alle Cookies eintragen."}), 400


@app.route("/debug", methods=["POST"])
def debug():
    try:
        session = get_session()
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 401
    today = date.today().isoformat()
    user_id = get_user_id(session)
    endpoints = {
        "user_id": user_id,
        "sleep": f"{GARMIN_API}/wellness-service/wellness/dailySleepData/{today}",
        "sleep_uid": f"{GARMIN_API}/wellness-service/wellness/dailySleepData/{user_id}/{today}" if user_id else None,
        "activities": f"{GARMIN_API}/activitylist-service/activities/search/activities",
        "hrv": f"{GARMIN_API}/hrv-service/hrv/{today}",
    }
    results = {"user_id": user_id}
    for name, url in endpoints.items():
        if not url or name == "user_id":
            continue
        try:
            r = session.get(url, timeout=10)
            results[name] = {"status": r.status_code, "preview": r.text[:300]}
        except Exception as e:
            results[name] = {"error": str(e)}
    return jsonify({"ok": True, "results": results})


@app.route("/health")
def health():
    return jsonify({"ok": True})


@app.route("/data", methods=["POST"])
def data():
    try:
        session = get_session()
        user_id = get_user_id(session)
        print(f"Fetching data for user_id: {user_id}")
        sleep = fetch_sleep(session, user_id=user_id)
        training = fetch_training(session, user_id=user_id)
        return jsonify({"ok": True, "sleep": sleep, "training": training})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 401
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
