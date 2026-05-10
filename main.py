import os
import asyncio
import httpx
import websockets
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Sentry")

# Config from .env
GOTIFY_URL = os.getenv("GOTIFY_URL", "http://localhost:80")
GOTIFY_TOKEN = os.getenv("GOTIFY_TOKEN", "")
BACKGROUND_IMAGE = os.getenv("BACKGROUND_IMAGE", "")
NOTIFICATION_OPACITY = os.getenv("NOTIFICATION_OPACITY", "0.70")
PRIORITY10_DURATION = os.getenv("PRIORITY10_DURATION", "30")
SERVICES_RAW = os.getenv("SERVICES", "")

# In-memory app name cache: {appid: name}
app_names: dict[int, str] = {}


def parse_services():
    services = []
    if not SERVICES_RAW.strip():
        return services
    for entry in SERVICES_RAW.split(","):
        entry = entry.strip()
        parts = entry.split("|", 1)
        if len(parts) == 2:
            services.append({"name": parts[0].strip(), "url": parts[1].strip()})
    return services


def gotify_ws_url():
    url = GOTIFY_URL.rstrip("/")
    url = url.replace("https://", "wss://").replace("http://", "ws://")
    return f"{url}/stream?token={GOTIFY_TOKEN}"


async def fetch_app_names():
    """Fetch and cache Gotify app names."""
    global app_names
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"{GOTIFY_URL.rstrip('/')}/application",
                headers={"X-Gotify-Key": GOTIFY_TOKEN}
            )
            r.raise_for_status()
            apps = r.json()
            app_names = {a["id"]: a["name"] for a in apps}
    except Exception as e:
        print(f"[sentry] Could not fetch app names: {e}")


@app.on_event("startup")
async def startup():
    await fetch_app_names()


@app.get("/api/config")
async def get_config():
    return {
        "background_image": BACKGROUND_IMAGE,
        "notification_opacity": float(NOTIFICATION_OPACITY),
        "priority10_duration": int(PRIORITY10_DURATION),
        "services": parse_services(),
    }


@app.get("/api/history")
async def get_history():
    """Fetch last 40 messages from Gotify, enriched with app names."""
    # Refresh app names in case new apps were added
    await fetch_app_names()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"{GOTIFY_URL.rstrip('/')}/message",
                params={"limit": 40},
                headers={"X-Gotify-Key": GOTIFY_TOKEN}
            )
            r.raise_for_status()
            data = r.json()
            messages = data.get("messages", [])
            # Enrich with appname
            for msg in messages:
                msg["appname"] = app_names.get(msg.get("appid"), "Sistema")
            # Newest first
            messages.sort(key=lambda m: m.get("date", ""), reverse=True)
            return {"messages": messages}
    except Exception as e:
        return {"messages": [], "error": str(e)}


@app.get("/api/stream")
async def stream_gotify():
    """SSE endpoint — conecta a Gotify por WS y hace relay al browser por HTTP."""

    async def event_generator():
        ws_url = gotify_ws_url()
        while True:
            try:
                async with websockets.connect(ws_url) as ws:
                    yield "event: status\ndata: connected\n\n"
                    async for message in ws:
                        yield f"data: {message}\n\n"
            except Exception:
                yield "event: status\ndata: disconnected\n\n"
                await asyncio.sleep(5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/ping")
async def ping_services():
    services = parse_services()

    async def check(service):
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(service["url"])
                online = r.status_code < 500
        except Exception:
            online = False
        return {"name": service["name"], "online": online}

    results = await asyncio.gather(*[check(s) for s in services])
    return {"services": list(results)}


# Serve static files (must be after API routes)
app.mount("/", StaticFiles(directory="static", html=True), name="static")
