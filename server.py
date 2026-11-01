"""FlirtUA Ads — генератор рекламних відео для TikTok / Reels / Shorts на fal.ai.

Окремий сервіс: сторінка за паролем, ключ fal.ai лише на сервері.
Браузер ходить у fal.ai через проксі /fal (тільки після входу й тільки до потрібних моделей),
а ролик збирається прямо в браузері.

Змінні середовища (Railway → Variables):
  FAL_KEY        — ключ fal.ai (обов'язково)
  APP_PASSWORD   — пароль входу (обов'язково)
  BOT_USERNAME   — ім'я бота без @, за замовчуванням FlirtUa_bot
  COUNTRIES      — країни через кому, за замовчуванням Іспанія, Польща, Німеччина, Чехія, Італія
  FIREBASE_JSON  — (необов'язково) ключ Firebase бота: тоді в рекламі будуть справжні цифри анкет
  ELEVENLABS_API_KEY — (рекомендовано) ключ ElevenLabs: озвучка напряму, будь-який голос з вашого акаунта,
                   одним дублем з таймкодами. Без нього — ElevenLabs через fal.ai (два стандартні голоси)
"""
import base64
import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from collections import Counter, defaultdict
from urllib.parse import urlparse

import aiohttp
from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

FAL_KEY = os.getenv("FAL_KEY", "").strip()
EL_KEY = (os.getenv("ELEVENLABS_API_KEY") or os.getenv("ELEVEN_API_KEY") or "").strip()
EL_API = "https://api.elevenlabs.io"
EL_MODELS = {"eleven_v3", "eleven_multilingual_v2"}
PASSWORD = os.getenv("APP_PASSWORD", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "FlirtUa_bot").strip().lstrip("@")
COUNTRIES = [c.strip() for c in os.getenv("COUNTRIES", "Іспанія, Польща, Німеччина, Чехія, Італія").split(",") if c.strip()]
TOKEN = hmac.new(PASSWORD.encode(), b"flirtua-ads-v1", hashlib.sha256).hexdigest() if PASSWORD else ""
DAY = 86400

# моделі, які може викликати сторінка (owner/app — перші дві частини шляху)
ALLOWED_APPS = {"fal-ai/any-llm", "openai/gpt-image-2", "fal-ai/kling-video", "fal-ai/elevenlabs", "CassetteAI/music-generator"}
FAL_HOSTS = ("fal.run",)
MEDIA_HOSTS = ("fal.media", "fal.ai", "fal.run", "storage.googleapis.com")
PASS_HEADERS = ("content-type", "accept", "x-fal-queue-priority", "x-fal-runner-hint", "x-fal-request-timeout", "x-fal-store-io")


def host_ok(host, allowed):
    host = (host or "").lower()
    return any(host == h or host.endswith("." + h) for h in allowed)


# ---------- справжні цифри анкет з Firebase бота (необов'язково) ----------
db = None
if os.getenv("FIREBASE_JSON"):
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
        firebase_admin.initialize_app(credentials.Certificate(json.loads(os.environ["FIREBASE_JSON"])))
        db = firestore.client()
        logging.info("✅ Firebase підключено — цифри анкет будуть справжні")
    except Exception as e:  # noqa: BLE001
        logging.warning(f"Firebase не підключився: {e}")

_counts = {"at": 0, "data": None}


async def profile_counts():
    if not db:
        return None
    if _counts["data"] is not None and time.time() - _counts["at"] < 600:
        return _counts["data"]

    def run():
        c = Counter()
        for d in db.collection("users").select(["country", "banned"]).stream():
            u = d.to_dict() or {}
            if not u.get("banned"):
                c[u.get("country") or ""] += 1
        return c
    try:
        c = await asyncio.to_thread(run)
        _counts.update(at=time.time(), data=c)
        return c
    except Exception as e:  # noqa: BLE001
        logging.warning(f"Firebase counts: {e}")
        return None


# ---------- вхід ----------
fails = defaultdict(list)


def authed(req):
    return bool(TOKEN) and hmac.compare_digest(req.cookies.get("ads_auth", ""), TOKEN)


LOGIN_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>Реклама FlirtUA</title><style>
:root{--paper:#f3f4f6;--card:#fff;--ink:#161b22;--muted:#637081;--line:#e1e5ea;--acc:#e0446a}
@media (prefers-color-scheme:dark){:root{--paper:#0f1318;--card:#171c23;--ink:#e8ecf0;--muted:#94a0ad;--line:#28303a}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--paper);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
form{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:24px;width:min(360px,calc(100% - 32px))}
h1{margin:0 0 4px;font-size:20px}p{margin:0 0 16px;color:var(--muted);font-size:14px}
input{width:100%;box-sizing:border-box;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:10px;padding:11px;font:15px inherit;margin-bottom:10px}
button{width:100%;border:0;border-radius:999px;padding:12px;background:var(--acc);color:#fff;font:600 15px system-ui;cursor:pointer}
#m{color:#c9373f;margin:10px 0 0;min-height:1em}</style></head><body>
<form id="f"><h1>🎬 Реклама FlirtUA</h1><p>Генератор роликов для TikTok, Reels и Shorts</p>
<input type="password" id="p" placeholder="Пароль" autocomplete="current-password" required autofocus><button>Войти</button><p id="m"></p></form>
<script>document.getElementById("f").onsubmit=async e=>{e.preventDefault();const r=await fetch("/login",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({password:document.getElementById("p").value})});const d=await r.json().catch(()=>({}));if(r.ok)location.href="/";else document.getElementById("m").textContent=d.error||"Ошибка"};</script>
</body></html>"""


async def login_page(req):
    if authed(req):
        raise web.HTTPFound("/")
    return web.Response(text=LOGIN_HTML, content_type="text/html", headers={"cache-control": "no-store"})


async def login(req):
    ip = req.headers.get("x-forwarded-for", "").split(",")[0].strip() or req.remote or "?"
    now = time.time()
    fails[ip] = [t for t in fails[ip] if now - t < 900]
    if len(fails[ip]) >= 10:
        return web.json_response({"error": "Слишком много попыток. Подождите 15 минут."}, status=429)
    if not PASSWORD:
        return web.json_response({"error": "Вход выключен: задайте APP_PASSWORD в Railway → Variables."}, status=403)
    body = await req.json()
    if not hmac.compare_digest(str(body.get("password", "")).encode(), PASSWORD.encode()):
        fails[ip].append(now)
        return web.json_response({"error": "Неверный пароль"}, status=401)
    res = web.json_response({"ok": True})
    secure = req.headers.get("x-forwarded-proto", req.scheme) == "https"
    res.set_cookie("ads_auth", TOKEN, max_age=60 * DAY, httponly=True, samesite="Strict", secure=secure)
    return res


async def logout(req):
    res = web.json_response({"ok": True})
    res.del_cookie("ads_auth")
    return res


# ---------- сторінка й API ----------
async def index(req):
    if not authed(req):
        raise web.HTTPFound("/login")
    with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
        html = f.read()
    return web.Response(text=html, content_type="text/html", headers={"cache-control": "no-store", "x-robots-tag": "noindex"})


async def info(req):
    if not authed(req):
        return web.json_response({"error": "Нужно войти"}, status=401)
    c = await profile_counts()
    return web.json_response({
        "falKey": bool(FAL_KEY),
        "bot": BOT_USERNAME,
        "countries": COUNTRIES,
        "profiles": {k: (c.get(k, 0) if c else 0) for k in COUNTRIES},
        "total": sum(c.values()) if c else 0,
        "realCounts": c is not None,
        "elevenlabs": bool(EL_KEY),
    })


# ---------- ElevenLabs напряму ----------
_voices = {"at": 0, "data": None}


def voice_out(v):
    lab = v.get("labels") or {}
    langs = [x.get("language") for x in (v.get("verified_languages") or []) if x.get("language")]
    return {"id": v.get("voice_id"), "name": v.get("name", ""), "category": v.get("category", ""),
            "gender": lab.get("gender", ""), "accent": lab.get("accent", ""), "age": lab.get("age", ""),
            "lang": lab.get("language", "") or ",".join(langs), "description": lab.get("description", "") or lab.get("descriptive", ""),
            "preview": v.get("preview_url", "")}


async def voices(req):
    if not authed(req):
        return web.json_response({"error": "Нужно войти"}, status=401)
    if not EL_KEY:
        return web.json_response({"error": "Нет ELEVENLABS_API_KEY"}, status=400)
    if _voices["data"] is None or time.time() - _voices["at"] > 300 or req.query.get("fresh"):
        s = await session()
        async with s.get(f"{EL_API}/v1/voices", headers={"xi-api-key": EL_KEY}) as r:
            if r.status != 200:
                return web.json_response({"error": f"ElevenLabs: {(await r.text())[:300]}"}, status=502)
            data = await r.json()
        _voices.update(at=time.time(), data=[voice_out(v) for v in data.get("voices", [])])
    return web.json_response({"voices": _voices["data"]})


def el_settings(model, b):
    if model == "eleven_v3":
        st = float(b.get("stability", 0.5))
        return {"stability": min((0.0, 0.5, 1.0), key=lambda x: abs(x - st))}  # v3: Creative / Natural / Robust
    return {"stability": float(b.get("stability", 0.45)), "similarity_boost": 0.8, "style": float(b.get("style", 0.3)), "use_speaker_boost": True}


async def tts(req):
    """Озвучка через ElevenLabs. timestamps=true → JSON з audio_base64 і таймкодами символів, інакше mp3."""
    if not authed(req):
        return web.json_response({"error": "Нужно войти"}, status=401)
    if not EL_KEY:
        return web.json_response({"error": "Нет ELEVENLABS_API_KEY"}, status=400)
    b = await req.json()
    text = str(b.get("text", "")).strip()
    vid = str(b.get("voice_id", "")).strip()
    model = b.get("model") if b.get("model") in EL_MODELS else "eleven_v3"
    if not text or len(text) > 3000 or not vid.replace("-", "").isalnum():
        return web.json_response({"error": "Нужен текст (до 3000 символов) и голос"}, status=400)
    body = {"text": text, "model_id": model, "voice_settings": el_settings(model, b)}
    if model == "eleven_v3":
        body["language_code"] = "uk"
    ts = bool(b.get("timestamps"))
    url = f"{EL_API}/v1/text-to-speech/{vid}" + ("/with-timestamps" if ts else "") + "?output_format=mp3_44100_128"
    s = await session()
    for attempt in range(2):
        async with s.post(url, headers={"xi-api-key": EL_KEY, "content-type": "application/json"}, json=body) as r:
            if r.status == 200:
                if ts:
                    d = await r.json()
                    return web.json_response({"audio_base64": d.get("audio_base64"), "alignment": d.get("alignment") or d.get("normalized_alignment")})
                return web.Response(body=await r.read(), content_type="audio/mpeg")
            err = await r.text()
        if attempt == 0 and "language_code" in body and "language" in err.lower():
            body.pop("language_code")  # модель не приймає мову — пробуємо без неї
            continue
        break
    try:
        msg = json.loads(err).get("detail", {})
        msg = msg.get("message") if isinstance(msg, dict) else msg
    except Exception:  # noqa: BLE001
        msg = err[:300]
    return web.json_response({"error": f"ElevenLabs: {msg}"}, status=502)


_session = None


async def session():
    global _session
    if not _session or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600))
    return _session


async def fal_proxy(req):
    """Проксі для @fal-ai/client (заголовок x-fal-target-url): додає ключ і пускає лише до потрібних моделей."""
    if not authed(req):
        return web.json_response({"error": "Нужно войти"}, status=401)
    if not FAL_KEY:
        return web.json_response({"error": "Нет FAL_KEY: добавьте ключ fal.ai в Railway → Variables"}, status=500)
    target = req.headers.get("x-fal-target-url", "")
    u = urlparse(target)
    if u.scheme != "https" or not host_ok(u.hostname, FAL_HOSTS):
        return web.json_response({"error": "Недопустимый адрес"}, status=400)
    app_id = "/".join(u.path.strip("/").split("/")[:2])
    if app_id not in ALLOWED_APPS:
        return web.json_response({"error": f"Модель {app_id} не разрешена"}, status=403)
    headers = {"authorization": f"Key {FAL_KEY}"}
    for h in PASS_HEADERS:
        if h in req.headers:
            headers[h] = req.headers[h]
    body = await req.read() if req.method in ("POST", "PUT") else None
    s = await session()
    try:
        async with s.request(req.method, target, headers=headers, data=body) as r:
            return web.Response(body=await r.read(), status=r.status, content_type=(r.content_type or "application/json"))
    except Exception as e:  # noqa: BLE001
        logging.warning(f"fal proxy {app_id}: {e}")
        return web.json_response({"error": f"fal.ai не ответил: {e}"}, status=502)


async def media(req):
    """Файли fal через наш сервер — щоб браузер міг зібрати з них ролик без проблем CORS."""
    if not authed(req):
        return web.json_response({"error": "Нужно войти"}, status=401)
    url = req.query.get("url", "")
    u = urlparse(url)
    if u.scheme != "https" or not host_ok(u.hostname, MEDIA_HOSTS):
        return web.json_response({"error": "Недопустимый адрес"}, status=400)
    s = await session()
    try:
        async with s.get(url) as r:
            if r.status != 200:
                return web.json_response({"error": f"файл не скачался ({r.status})"}, status=502)
            data = await r.read()
            if len(data) > 80 * 1024 * 1024:
                return web.json_response({"error": "файл слишком большой"}, status=413)
            return web.Response(body=data, content_type=r.content_type or "application/octet-stream", headers={"cache-control": "private, max-age=86400"})
    except Exception as e:  # noqa: BLE001
        return web.json_response({"error": str(e)}, status=502)


async def health(req):
    return web.json_response({"ok": True, "falKey": bool(FAL_KEY), "password": bool(PASSWORD), "firebase": db is not None, "elevenlabs": bool(EL_KEY)})


def make_app():
    app = web.Application(client_max_size=10 * 1024 * 1024)
    r = app.router
    r.add_get("/", index)
    r.add_get("/login", login_page)
    r.add_post("/login", login)
    r.add_post("/logout", logout)
    r.add_get("/api/info", info)
    r.add_get("/api/media", media)
    r.add_route("*", "/fal", fal_proxy)
    r.add_get("/api/voices", voices)
    r.add_post("/api/tts", tts)
    r.add_get("/health", health)
    return app


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    logging.info(f"🎬 FlirtUA Ads: порт {port}" + ("" if FAL_KEY else " — нет FAL_KEY") + ("" if PASSWORD else " — нет APP_PASSWORD, вход закрыт"))
    web.run_app(make_app(), port=port, print=None)
