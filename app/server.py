"""SentinelNVR backend: login, PTZ (ONVIF), recordings timeline/playback/export, go2rtc proxy, faces."""
import asyncio
import contextlib
import hashlib
import hmac
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

import faces

CAM = os.getenv("CAMERA_IP", "192.168.1.50")
NVR = Path(os.getenv("NVR_DIR", "/mnt/nvr"))
SEG = int(os.getenv("SEGMENT_SECONDS", "120"))
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "30"))
G2G = os.getenv("GO2RTC_URL", "http://127.0.0.1:1984")
PROFILE = os.getenv("PTZ_PROFILE", "Profile_1")
PASSWORD = os.environ["HOMECAM_PASSWORD"]
SECRET = os.environ["HOMECAM_SECRET"].encode()
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") == "1"
STREAMS = {"homecam", "homecam_sub", "homecam_h264"}
G2G_FILES = {"video-stream.js", "video-rtc.js", "api/frame.jpeg"}
G2G_RTSP = os.getenv("GO2RTC_RTSP", "rtsp://127.0.0.1:8554")
LIVE_DIR = Path(os.getenv("LIVE_DIR", "/dev/shm/homecam-live"))
LIVE_IDLE = 30  # seconds without a request before the live HLS ffmpeg stops
SESSION_TTL = 30 * 86400
STATIC = Path(__file__).parent / "static"
NS_PTZ = "http://www.onvif.org/ver20/ptz/wsdl"
NS_T = "http://www.onvif.org/ver10/schema"



@contextlib.asynccontextmanager
async def lifespan(_app):
    await faces.start()
    reaper = asyncio.create_task(_live_reaper())
    yield
    reaper.cancel()
    for src in list(_live):
        await _live_stop(src)


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
client = httpx.AsyncClient(timeout=6)
app.include_router(faces.router)


# ---------------------------------------------------------------- auth
def make_token() -> str:
    exp = str(int(time.time()) + SESSION_TTL)
    return f"{exp}.{hmac.new(SECRET, exp.encode(), hashlib.sha256).hexdigest()}"


def valid_token(tok: str) -> bool:
    try:
        exp, sig = tok.split(".")
        good = hmac.new(SECRET, exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and int(exp) > time.time()
    except (ValueError, AttributeError):
        return False


@app.middleware("http")
async def auth(request: Request, call_next):
    p = request.url.path
    if p.startswith(("/api/", "/rec/", "/go2rtc/")) and p != "/api/login":
        if not valid_token(request.cookies.get("hc", "")):
            return JSONResponse({"error": "auth"}, status_code=401)
    return await call_next(request)


_fails: dict = {}


@app.post("/api/login")
async def login(request: Request):
    ip = request.client.host if request.client else "?"
    now = time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 300]
    if len(recent) >= 5:
        raise HTTPException(429, "Too many attempts, wait 5 minutes")
    body = await request.json()
    if not hmac.compare_digest(str(body.get("password", "")).encode(), PASSWORD.encode()):
        recent.append(now)
        _fails[ip] = recent
        await asyncio.sleep(1)
        raise HTTPException(401, "Wrong password")
    _fails.pop(ip, None)
    resp = JSONResponse({"ok": True})
    resp.set_cookie("hc", make_token(), max_age=SESSION_TTL, httponly=True,
                    samesite="lax", secure=COOKIE_SECURE)
    return resp


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("hc")
    return resp


# ---------------------------------------------------------------- PTZ (ONVIF)
async def onvif(service: str, body: str) -> str:
    env = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
           f"<s:Body>{body}</s:Body></s:Envelope>")
    try:
        r = await client.post(f"http://{CAM}/onvif/{service}", content=env,
                              headers={"Content-Type": "application/soap+xml"})
    except httpx.HTTPError:
        raise HTTPException(502, "camera unreachable")
    if r.status_code >= 400 or "Fault" in r.text:
        raise HTTPException(502, "camera rejected command")
    return r.text


_stop_task = None


async def _stop():
    await onvif("PTZ", f'<Stop xmlns="{NS_PTZ}"><ProfileToken>{PROFILE}</ProfileToken>'
                       "<PanTilt>true</PanTilt><Zoom>true</Zoom></Stop>")


async def _auto_stop(delay: float):
    await asyncio.sleep(delay)
    try:
        await _stop()
    except Exception:
        pass


@app.post("/api/ptz/move")
async def ptz_move(request: Request):
    """Continuous move; always auto-stops (default 3 s, max 5 s) so a dropped client can't leave it spinning."""
    global _stop_task
    b = await request.json()
    x, y, z = (max(-1.0, min(1.0, float(b.get(k, 0)))) for k in ("pan", "tilt", "zoom"))
    vel = f'<PanTilt xmlns="{NS_T}" x="{x}" y="{y}"/>'
    if z:
        vel += f'<Zoom xmlns="{NS_T}" x="{z}"/>'
    await onvif("PTZ", f'<ContinuousMove xmlns="{NS_PTZ}"><ProfileToken>{PROFILE}</ProfileToken>'
                       f"<Velocity>{vel}</Velocity></ContinuousMove>")
    if _stop_task:
        _stop_task.cancel()
    _stop_task = asyncio.create_task(_auto_stop(min(float(b.get("seconds", 3)), 5)))
    return {"ok": True}


@app.post("/api/ptz/stop")
async def ptz_stop():
    global _stop_task
    if _stop_task:
        _stop_task.cancel()
    await _stop()
    return {"ok": True}


@app.get("/api/ptz/presets")
async def ptz_presets():
    # This camera's firmware drops the connection on GetPresets; "no presets" is the honest answer then.
    try:
        t = await onvif("PTZ", f'<GetPresets xmlns="{NS_PTZ}"><ProfileToken>{PROFILE}</ProfileToken></GetPresets>')
    except HTTPException:
        return []
    found = re.findall(r'<\w+:Preset[^>]*token="([^"]+)"[^>]*>(?:\s*<\w+:Name>([^<]*)<)?', t)
    return [{"token": k, "name": n or k} for k, n in found]


@app.post("/api/ptz/preset/{token}")
async def ptz_goto(token: str):
    if not re.fullmatch(r"[\w-]{1,32}", token):
        raise HTTPException(400, "bad token")
    await onvif("PTZ", f'<GotoPreset xmlns="{NS_PTZ}"><ProfileToken>{PROFILE}</ProfileToken>'
                       f"<PresetToken>{token}</PresetToken></GotoPreset>")
    return {"ok": True}


@app.post("/api/ptz/preset-set")
async def ptz_set(request: Request):
    name = re.sub(r"[^\w -]", "", str((await request.json()).get("name", "")))[:24] or "preset"
    await onvif("PTZ", f'<SetPreset xmlns="{NS_PTZ}"><ProfileToken>{PROFILE}</ProfileToken>'
                       f"<PresetName>{name}</PresetName></SetPreset>")
    return {"ok": True}


# ---------------------------------------------------------------- go2rtc proxy (live)
@app.get("/go2rtc/{path:path}")
async def g2g_http(path: str, request: Request):
    src = request.query_params.get("src")
    if path not in G2G_FILES or (src is not None and src not in STREAMS):
        raise HTTPException(404)
    r = await client.get(f"{G2G}/{path}", params=dict(request.query_params), timeout=15)
    return Response(r.content, r.status_code, media_type=r.headers.get("content-type"))


# ---------------------------------------------------------------- live HLS (native iOS app)
# go2rtc's own HLS uses ~0.2 s fragments that AVPlayer never starts on, so the app gets HLS from an
# on-demand ffmpeg instead: video copied as-is (H.265, tagged hvc1 for Apple), no audio,
# 1 s fMP4 segments in RAM. It starts on the first playlist request and stops after LIVE_IDLE seconds idle.
LIVE_FILE_RE = re.compile(r"^(index\.m3u8|init\.mp4|seg\d+\.m4s)$")
_live: dict = {}  # stream -> {"proc", "dir", "last"}
_live_lock = asyncio.Lock()


async def _live_start(src: str) -> dict:
    d = LIVE_DIR / src
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    tag = ["-tag:v", "hvc1"] if src != "homecam_h264" else []
    # Video only: the camera's PCMA audio arrives intermittently (seemingly only when there is sound),
    # and while it is silent ffmpeg holds every segment back waiting for audio, so live view would freeze.
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-v", "error", "-rtsp_transport", "tcp", "-timeout", "10000000",
        "-i", f"{G2G_RTSP}/{src}", "-map", "0:v:0", "-c:v", "copy", *tag, "-an",
        "-f", "hls", "-hls_time", "1", "-hls_list_size", "6",
        "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4",
        "-hls_segment_filename", str(d / "seg%d.m4s"),
        "-hls_flags", "delete_segments+independent_segments+omit_endlist", str(d / "index.m3u8"),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    return {"proc": proc, "dir": d, "last": time.time(), "started": time.time()}


async def _live_stop(src: str):
    s = _live.pop(src, None)
    if not s:
        return
    if s["proc"].returncode is None:
        s["proc"].kill()
        await s["proc"].wait()
    shutil.rmtree(s["dir"], ignore_errors=True)


def _live_stalled(s: dict) -> bool:
    """ffmpeg can hang on the RTSP handshake: no new file for 20 s means start over."""
    newest = s["started"]
    with contextlib.suppress(OSError):
        newest = max([newest, *(p.stat().st_mtime for p in s["dir"].iterdir())])
    return time.time() - newest > 20


async def _live_reaper():
    while True:
        await asyncio.sleep(5)
        for src, s in list(_live.items()):
            if time.time() - s["last"] > LIVE_IDLE or s["proc"].returncode is not None or _live_stalled(s):
                async with _live_lock:
                    await _live_stop(src)


@app.get("/api/live/{src}/{name}")
async def live_hls(src: str, name: str):
    if src not in STREAMS or not LIVE_FILE_RE.match(name):
        raise HTTPException(404)
    async with _live_lock:
        s = _live.get(src)
        if not s or s["proc"].returncode is not None:
            await _live_stop(src)
            s = _live[src] = await _live_start(src)
    s["last"] = time.time()
    p = s["dir"] / name
    if name == "index.m3u8":
        for _ in range(100):  # the first playlist appears once a segment is complete
            if p.exists():
                break
            if s["proc"].returncode is not None:
                raise HTTPException(502, "live stream failed to start")
            await asyncio.sleep(0.15)
        else:
            async with _live_lock:  # likely hung on the RTSP handshake: the next request starts afresh
                if _live.get(src) is s:
                    await _live_stop(src)
            raise HTTPException(504, "camera stream not ready")
        return Response(p.read_bytes(), media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-cache"})
    try:
        return Response(p.read_bytes(), media_type="video/mp4", headers={"Cache-Control": "no-cache"})
    except FileNotFoundError:
        raise HTTPException(404)


@app.get("/api/snapshot")
async def snapshot():
    r = await client.get(f"{G2G}/api/frame.jpeg", params={"src": "homecam"}, timeout=20)
    if r.status_code != 200:
        raise HTTPException(502, "no frame")
    name = datetime.now().strftime("homecam-%Y%m%d-%H%M%S.jpg")
    return Response(r.content, media_type="image/jpeg",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.websocket("/go2rtc/api/ws")
async def g2g_ws(ws: WebSocket):
    src = ws.query_params.get("src")
    if not valid_token(ws.cookies.get("hc", "")) or src not in STREAMS:
        await ws.close(code=1008)
        return
    await ws.accept()
    url = G2G.replace("http", "ws", 1) + f"/api/ws?src={src}"
    try:
        async with websockets.connect(url, max_size=None) as up:
            async def c2u():
                while True:
                    m = await ws.receive()
                    if m["type"] == "websocket.disconnect":
                        return
                    if m.get("text") is not None:
                        await up.send(m["text"])
                    elif m.get("bytes") is not None:
                        await up.send(m["bytes"])

            async def u2c():
                async for m in up:
                    if isinstance(m, str):
                        await ws.send_text(m)
                    else:
                        await ws.send_bytes(m)

            tasks = [asyncio.create_task(c2u()), asyncio.create_task(u2c())]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in tasks:
                t.cancel()
    except Exception:
        pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------- recordings
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NAME_RE = re.compile(r"^\d{2}-\d{2}-\d{2}\.mp4$")


def day_epoch(day: str) -> float:
    return datetime.strptime(day, "%Y-%m-%d").timestamp()


def seg_dir(day: str) -> Path:
    y, m, d = day.split("-")
    return NVR / y / m / d


def list_segments(day: str) -> list:
    if not DAY_RE.match(day):
        raise HTTPException(400, "bad day")
    base, out, now = day_epoch(day), [], time.time()
    d = seg_dir(day)
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.mp4")):
        if not NAME_RE.match(p.name):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        h, m, s = (int(x) for x in p.stem.split("-"))
        start = base + h * 3600 + m * 60 + s
        end = min(max(st.st_mtime, start + 1), start + SEG * 2.5)
        out.append({"name": p.name, "day": day, "start": start, "end": end,
                    "size": st.st_size, "live": now - st.st_mtime < 15})
    return out


def list_days() -> list:
    days = []
    for y in NVR.glob("[0-9][0-9][0-9][0-9]"):
        for m in y.glob("[0-9][0-9]"):
            for d in m.glob("[0-9][0-9]"):
                if any(d.glob("*.mp4")):
                    days.append(f"{y.name}-{m.name}-{d.name}")
    return sorted(days)


@app.get("/api/days")
async def api_days():
    return await asyncio.to_thread(list_days)


@app.get("/api/segments")
async def api_segments(day: str):
    segs = await asyncio.to_thread(list_segments, day)
    start = day_epoch(day)
    return {"day": day, "day_start": start, "segments": segs}


@app.get("/rec/{day}/{name}")
async def rec(day: str, name: str, dl: int = 0, h264: int = 0):
    if not DAY_RE.match(day) or not NAME_RE.match(name):
        raise HTTPException(400, "bad path")
    path = seg_dir(day) / name
    if not path.is_file():
        raise HTTPException(404)
    if h264:
        return await _transcode_stream(path)
    fn = f"homecam-{day}-{name}" if dl else None
    return FileResponse(path, media_type="video/mp4", filename=fn)


THUMBS = Path(os.getenv("THUMB_DIR", "/var/cache/homecam/thumbs"))
_thumb_sem = asyncio.Semaphore(2)


_thumb_fail: dict = {}  # (day, name) -> (size, mtime) of a segment ffmpeg could not get a frame from
THUMB_TRIES = 4  # the requested segment plus the next few in the same hour


async def _grab_frame(src: Path, dst: Path) -> bool:
    tmp = dst.with_suffix(".tmp.jpg")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-y", "-i", str(src), "-frames:v", "1",
        "-vf", "scale=480:-2", "-q:v", "6", str(tmp),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(proc.wait(), 20)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
    if proc.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(dst)
    return True


@app.get("/api/thumb/{day}/{name}")
async def thumb(day: str, name: str):
    """First frame of a segment as a small JPEG, cached on local disk (the UI asks for ~1 per hour).
    If that segment has no decodable frame, the next few segments of the same hour are tried; segments
    that failed are remembered (until they change) so the UI's periodic re-renders don't re-run ffmpeg."""
    if not DAY_RE.match(day) or not NAME_RE.match(name):
        raise HTTPException(400, "bad path")
    d = seg_dir(day)
    if not (d / name).is_file():
        raise HTTPException(404)
    out = THUMBS / day / (name[:-4] + ".jpg")
    if not out.exists():
        async with _thumb_sem:
            if not out.exists():
                try:
                    out.parent.mkdir(parents=True, exist_ok=True)
                except OSError:
                    raise HTTPException(503, "thumbnail cache not writable")
                names = await asyncio.to_thread(
                    lambda: sorted(p.name for p in d.glob(name[:3] + "*.mp4") if NAME_RE.match(p.name)))
                for cand in [n for n in names if n >= name][:THUMB_TRIES]:
                    try:
                        st = (d / cand).stat()
                    except OSError:
                        continue
                    sig = (st.st_size, st.st_mtime)
                    if _thumb_fail.get((day, cand)) == sig:
                        continue
                    if await _grab_frame(d / cand, out):
                        break
                    _thumb_fail[(day, cand)] = sig
                else:
                    raise HTTPException(404, "no frame")
    return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})


async def _transcode_stream(path: Path):
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-i", str(path), "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "26", "-c:a", "aac", "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

    async def gen():
        try:
            while chunk := await proc.stdout.read(65536):
                yield chunk
        finally:
            if proc.returncode is None:
                proc.kill()
    return StreamingResponse(gen(), media_type="video/mp4")


@app.get("/api/export")
async def export(start: float, end: float, h264: int = 0):
    if end <= start or end - start > 1800:
        raise HTTPException(400, "range must be 1s..30min")
    d0 = datetime.fromtimestamp(start).date() - timedelta(days=1)
    d1 = datetime.fromtimestamp(end).date()
    segs = []
    d = d0
    while d <= d1:
        segs += await asyncio.to_thread(list_segments, d.isoformat())
        d += timedelta(days=1)
    segs = [s for s in segs if s["end"] > start and s["start"] < end]
    if not segs:
        raise HTTPException(404, "no recordings in range")
    tmp = Path(tempfile.mkdtemp(prefix="hcexp-"))
    lst, out = tmp / "list.txt", tmp / "clip.mp4"
    lst.write_text("".join(f"file '{seg_dir(s['day']) / s['name']}'\n" for s in segs))
    codec = (["-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-c:a", "aac"]
             if h264 else ["-c", "copy"])
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
        "-ss", str(max(0.0, start - segs[0]["start"])), "-t", str(end - start), *codec,
        "-movflags", "+faststart", str(out),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(proc.wait(), 300)
    except asyncio.TimeoutError:
        proc.kill()
    if proc.returncode != 0 or not out.exists():
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(500, "export failed")
    name = datetime.fromtimestamp(start).strftime("homecam-%Y%m%d-%H%M%S.mp4")
    return FileResponse(out, media_type="video/mp4", filename=name,
                        background=BackgroundTask(shutil.rmtree, tmp, ignore_errors=True))


def _status() -> dict:
    mounted = (NVR / ".homecam").exists()
    st = {"nvr_ok": mounted, "retention_days": RETENTION_DAYS, "recording": False,
          "last_segment": None, "free_gb": None, "total_gb": None}
    if mounted:
        du = shutil.disk_usage(NVR)
        st["free_gb"], st["total_gb"] = round(du.free / 1e9, 1), round(du.total / 1e9, 1)
        newest = 0.0
        for day in (datetime.now().date(), datetime.now().date() - timedelta(days=1)):
            for s in list_segments(day.isoformat()):
                newest = max(newest, s["end"])
        if newest:
            st["last_segment"] = newest
            st["recording"] = time.time() - newest < 45
    return st


@app.get("/api/status")
async def status():
    st = await asyncio.to_thread(_status)
    try:
        r = await client.get(f"{G2G}/api/streams", timeout=2)
        st["go2rtc"] = r.status_code == 200
    except httpx.HTTPError:
        st["go2rtc"] = False
    st["faces"] = faces.status()
    return st


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
