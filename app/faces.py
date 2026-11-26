"""Motion, people, animals and faces for SentinelNVR (CPU only).

  main stream -> one persistent ffmpeg at 2 fps (BGR 1080p)
     every frame : drop decoder garbage (grey frames after joining mid-GOP) -> 160x90 frame difference = motion
     during an event (motion, or a person/animal still in view):
        YOLOX-s @416 every ~1.5 s  -> person / animal boxes      (worker thread 1)
        YuNet @1280 every frame     -> faces, pose + sharpness -> SFace embedding -> match  (worker thread 2)
  Everything is stored per event (SQLite + JPEG crops). Names come only from faces the user named; other faces are
  matched against the good-quality named samples (top-2 mean, with a margin over the next person).
The camera's ONVIF PullPoint rejects subscriptions, so motion is computed here.
"""
import asyncio
import contextlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

log = logging.getLogger("uvicorn.error")

RTSP = os.getenv("GO2RTC_RTSP", "rtsp://127.0.0.1:8554")
STREAM = os.getenv("FACE_STREAM", "homecam")
DATA = Path(os.getenv("FACES_DIR", "/var/lib/homecam/faces"))
MODELS = Path(os.getenv("MODEL_DIR", "/opt/homecam/models"))
NVR = Path(os.getenv("NVR_DIR", "/mnt/nvr"))
FRAME_W, FRAME_H = (int(v) for v in os.getenv("FACE_FRAME", "1920x1080").split("x"))
FPS = float(os.getenv("FACE_FPS", "2"))
DETECT_W = int(os.getenv("FACE_DETECT_WIDTH", "1280"))
MIN_SCORE = float(os.getenv("FACE_MIN_SCORE", "0.80"))
MIN_SIZE = int(os.getenv("FACE_MIN_SIZE", "36"))
MATCH = float(os.getenv("FACE_MATCH", "0.38"))           # top-2 mean cosine to a person's good samples
MARGIN = float(os.getenv("FACE_MARGIN", "0.05"))         # ...and this much better than the next person
GOOD_Q = 0.3                                              # faces below this are kept but never used as samples
SAME = 0.45                                               # same person again within one event
OBJECTS_ON = os.getenv("OBJECTS_ENABLED", "1") == "1"
OBJ_SIZE = int(os.getenv("OBJECT_SIZE", "416"))
OBJ_EVERY = float(os.getenv("OBJECT_INTERVAL", "1.5"))
MOTION_LEVEL = int(os.getenv("MOTION_LEVEL", "25"))
MOTION_AREA = float(os.getenv("MOTION_AREA", "0.006"))
MOTION_GAP = float(os.getenv("MOTION_GAP", "10"))
STILL_MAX = 120          # a person/animal keeps an event open this long after the last real motion
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "30"))
MAX_FACES = 8
MW, MH = 160, 90
YUNET = MODELS / "face_detection_yunet_2023mar.onnx"
SFACE = MODELS / "face_recognition_sface_2021dec.onnx"
YOLOX = MODELS / "object_detection_yolox_2022nov.onnx"
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FILE_RE = re.compile(r"^[efo]\d+\.jpg$")
# COCO ids YOLOX reports that matter here, with their minimum confidence
CLASSES = {0: ("person", 0.50), 14: ("bird", 0.50), 15: ("cat", 0.45), 16: ("dog", 0.45), 17: ("horse", 0.5),
           18: ("sheep", 0.5), 19: ("cow", 0.5), 21: ("bear", 0.5)}

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, day TEXT, start REAL, end REAL,
  faces INTEGER DEFAULT 0, thumb TEXT);
CREATE INDEX IF NOT EXISTS ev_day ON events(day);
CREATE TABLE IF NOT EXISTS people(id INTEGER PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS faces(id INTEGER PRIMARY KEY, event_id INTEGER, day TEXT, ts REAL, score REAL,
  size INTEGER, quality REAL, emb BLOB, file TEXT, person_id INTEGER, match_id INTEGER, match_sim REAL);
CREATE INDEX IF NOT EXISTS f_ev ON faces(event_id);
CREATE INDEX IF NOT EXISTS f_day ON faces(day);
CREATE TABLE IF NOT EXISTS objects(id INTEGER PRIMARY KEY, event_id INTEGER, day TEXT, ts REAL, label TEXT,
  conf REAL, n INTEGER, file TEXT);
CREATE INDEX IF NOT EXISTS o_ev ON objects(event_id);
CREATE INDEX IF NOT EXISTS o_day ON objects(day);
"""
MIGRATIONS = [("faces", "q", "REAL"), ("faces", "yaw", "REAL"), ("faces", "sharp", "REAL"),
              ("faces", "reject", "TEXT DEFAULT ''"), ("people", "cover", "INTEGER"), ("people", "created", "REAL"),
              ("events", "fixed", "INTEGER DEFAULT 0")]


class DB:
    def __init__(self, path: Path):
        self.c = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.executescript(SCHEMA)
        for table, col, typ in MIGRATIONS:
            if col not in [r["name"] for r in self.c.execute(f"PRAGMA table_info({table})")]:
                self.c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        # People made before `created` existed: date them by their earliest face.
        self.c.execute("UPDATE people SET created=COALESCE((SELECT MIN(ts) FROM faces WHERE person_id=people.id "
                       "OR match_id=people.id), strftime('%s','now')) WHERE created IS NULL")
        self.lock = threading.Lock()

    def q(self, sql, args=()):
        with self.lock:
            return self.c.execute(sql, args).fetchall()

    def x(self, sql, args=()):
        with self.lock:
            return self.c.execute(sql, args).lastrowid

    def many(self, sql, rows):
        with self.lock:
            self.c.executemany(sql, rows)


def corrupt(frame) -> bool:
    """HEVC frames decoded without their reference picture come out mostly flat grey (128,128,128)."""
    s = frame[::16, ::16].astype("int16")
    return float((abs(s - 128) <= 6).all(2).mean()) > 0.35


class Engine:
    """OpenCV models. Imported lazily so the app still runs if opencv/models are missing."""

    def __init__(self):
        import cv2
        import numpy as np
        self.cv2, self.np = cv2, np
        cv2.setNumThreads(1)  # 2 vCPUs: faces and objects each get one worker thread
        self.det = cv2.FaceDetectorYN.create(str(YUNET), "", (DETECT_W, DETECT_W * 9 // 16), MIN_SCORE, 0.3, 20)
        self.rec = cv2.FaceRecognizerSF.create(str(SFACE), "")
        self.obj = None
        if OBJECTS_ON and YOLOX.exists():
            self.obj = cv2.dnn.readNet(str(YOLOX))
            g, s = [], []
            for st in (8, 16, 32):
                n = OBJ_SIZE // st
                xv, yv = np.meshgrid(np.arange(n), np.arange(n))
                g.append(np.stack((xv, yv), 2).reshape(-1, 2))
                s.append(np.full((n * n, 1), st))
            self.grid, self.stride = np.concatenate(g).astype(np.float32), np.concatenate(s).astype(np.float32)
        self.known = None
        self.relearn_rows([])

    # ---- faces
    def quality(self, f, aligned):
        """0..1 from pose (eye spacing, nose between the eyes) and sharpness. Back/top of head -> ~0."""
        cv2, np = self.cv2, self.np
        w = float(f[2])
        ex0, ex1 = sorted([float(f[4]), float(f[6])])
        eye_d = (ex1 - ex0) / max(w, 1)
        yaw = (float(f[8]) - ex0) / max(ex1 - ex0, 1)          # 0.5 = looking at the camera
        sharp = float(cv2.Laplacian(cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
        pose = max(0.0, 1 - abs(yaw - 0.5) / 0.6) * min(1.0, max(0.0, (eye_d - 0.15) / 0.2))
        q = pose * min(1.0, sharp / 400) * min(1.0, float(min(f[2], f[3])) / 80)
        return round(q, 3), round(yaw, 2), round(sharp)

    def faces(self, frame) -> list:
        cv2, np = self.cv2, self.np
        h, w = frame.shape[:2]
        s = min(1.0, DETECT_W / w)
        small = cv2.resize(frame, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA) if s < 1 else frame
        self.det.setInputSize((small.shape[1], small.shape[0]))
        _, found = self.det.detect(small)
        out = []
        for f in (found if found is not None else []):
            f = f.copy()
            f[:14] /= s
            x, y, fw, fh = (float(v) for v in f[:4])
            score = float(f[14])
            if score < MIN_SCORE or min(fw, fh) < MIN_SIZE:
                continue
            aligned = self.rec.alignCrop(frame, f)
            q, yaw, sharp = self.quality(f, aligned)
            if q <= 0.02 and (abs(yaw - 0.5) > 1.2):   # back of a head / hair, not a face
                continue
            emb = self.rec.feature(aligned).flatten().astype(np.float32)
            emb /= float(np.linalg.norm(emb)) + 1e-9
            out.append({"score": score, "size": int(min(fw, fh)), "q": q, "yaw": yaw, "sharp": sharp,
                        "emb": emb, "jpg": self.crop(frame, x, y, fw, fh, 0.45, 256)})
        return out

    def crop(self, frame, x, y, w, h, pad, size) -> bytes:
        cv2 = self.cv2
        H, W = frame.shape[:2]
        m = pad * max(w, h)
        x0, y0, x1, y1 = max(0, int(x - m)), max(0, int(y - m)), min(W, int(x + w + m)), min(H, int(y + h + m))
        c = frame[y0:y1, x0:x1]
        if max(c.shape[:2]) > size:
            k = size / max(c.shape[:2])
            c = cv2.resize(c, (max(1, round(c.shape[1] * k)), max(1, round(c.shape[0] * k))), interpolation=cv2.INTER_AREA)
        return cv2.imencode(".jpg", c, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tobytes()

    def thumb(self, frame) -> bytes:
        cv2 = self.cv2
        h, w = frame.shape[:2]
        small = cv2.resize(frame, (640, round(h * 640 / w)), interpolation=cv2.INTER_AREA)
        return cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()

    # ---- people / animals
    def objects(self, frame) -> list:
        if self.obj is None:
            return []
        cv2, np = self.cv2, self.np
        h, w = frame.shape[:2]
        k = min(OBJ_SIZE / h, OBJ_SIZE / w)
        canvas = np.full((OBJ_SIZE, OBJ_SIZE, 3), 114, np.uint8)
        nh, nw = int(h * k), int(w * k)
        canvas[:nh, :nw] = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
        self.obj.setInput(cv2.dnn.blobFromImage(canvas))
        p = self.obj.forward()[0]
        cls = p[:, 5:].argmax(1)
        conf = p[:, 4] * p[np.arange(len(p)), 5 + cls]
        keep = np.array([c in CLASSES and v >= CLASSES[c][1] for c, v in zip(cls, conf)], bool)
        if not keep.any():
            return []
        p, cls, conf = p[keep], cls[keep], conf[keep]
        xy = (p[:, :2] + self.grid[keep]) * self.stride[keep]
        wh = np.exp(p[:, 2:4]) * self.stride[keep]
        boxes = np.column_stack((xy - wh / 2, wh)) / k
        idx = cv2.dnn.NMSBoxes(boxes.tolist(), conf.tolist(), 0.3, 0.45)
        out = []
        for i in np.array(idx).flatten():
            x, y, bw, bh = (float(v) for v in boxes[i])
            out.append({"label": CLASSES[int(cls[i])][0], "conf": round(float(conf[i]), 2), "box": (x, y, bw, bh)})
        return out

    # ---- recognition
    def relearn_rows(self, rows):
        np = self.np
        pids = np.array([r["person_id"] for r in rows], np.int64)
        embs = np.array([np.frombuffer(r["emb"], np.float32) for r in rows], np.float32).reshape(-1, 128)
        qs = np.array([r["q"] if r["q"] is not None else 0.5 for r in rows], np.float32)
        groups = {}
        for pid in set(pids.tolist()):
            idx = np.where(pids == pid)[0]
            good = idx[qs[idx] >= GOOD_Q]
            groups[pid] = good if len(good) else idx   # a person with only poor samples still gets matched
        self.known = (embs, groups)

    def match(self, emb, q=1.0, reject=()):
        embs, groups = self.known
        if not groups:
            return None, None
        sims = embs @ emb
        scored = []
        for pid, idx in groups.items():
            if pid in reject:
                continue
            top = self.np.sort(sims[idx])[-2:]
            scored.append((float(top.mean()), pid))
        if not scored:
            return None, None
        scored.sort(reverse=True)
        best, pid = scored[0]
        second = scored[1][0] if len(scored) > 1 else -1.0
        need = MATCH if q >= GOOD_Q else MATCH + 0.1     # poor shots need a clearer resemblance
        if best >= need and best - second >= MARGIN:
            return pid, best
        return None, None

    def relearn(self, db: DB):
        """Reload the named samples and re-match every unnamed face."""
        np = self.np
        self.relearn_rows(db.q("SELECT person_id, emb, q FROM faces WHERE person_id IS NOT NULL"))
        upd = []
        for r in db.q("SELECT id, emb, q, reject FROM faces WHERE person_id IS NULL"):
            rej = {int(v) for v in (r["reject"] or "").split(",") if v}
            pid, sim = self.match(np.frombuffer(r["emb"], np.float32), r["q"] if r["q"] is not None else 0.5, rej)
            upd.append((pid, sim, r["id"]))
        db.many("UPDATE faces SET match_id=?, match_sim=? WHERE id=?", upd)


class Motion:
    """Running-average background on a 160x90 grey picture."""

    def __init__(self, np):
        self.np, self.bg, self.hits = np, None, 0
        self.mask = np.ones((MH, MW), bool)
        self.mask[: MH // 10, int(MW * 0.68):] = False  # burned-in clock, top right

    def feed(self, small) -> bool:
        np = self.np
        f = small.astype(np.float32)
        if self.bg is None:
            self.bg = f
            return False
        ratio = float((np.abs(f - self.bg) > MOTION_LEVEL)[self.mask].mean())
        if ratio > 0.5:  # IR switch, exposure jump or PTZ move: relearn instead of firing
            self.bg, self.hits = f, 0
            return False
        self.bg += 0.1 * (f - self.bg)
        self.hits = self.hits + 1 if ratio > MOTION_AREA else 0
        return self.hits >= 2  # two frames in a row (1 s) filters out single-frame noise


async def _ffmpeg(stream: str, vf: str, pix: str):
    return await asyncio.create_subprocess_exec(
        # "?video" asks go2rtc for the video track only; with a small probe the first frame arrives in ~2 s.
        "ffmpeg", "-nostdin", "-v", "quiet", "-rtsp_transport", "tcp", "-probesize", "200000",
        "-analyzeduration", "500000", "-i", f"{RTSP}/{stream}?video",
        "-an", "-vf", vf, "-pix_fmt", pix, "-f", "rawvideo", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)


async def _kill(proc):
    if proc.returncode is None:
        proc.kill()
    with contextlib.suppress(Exception):
        await proc.wait()


def _write(day, name, data: bytes):
    d = DATA / day
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(d / name)


class Watcher:
    def __init__(self, db: DB, eng: Engine):
        self.db, self.eng = db, eng
        self.ev = None
        self.task = None
        self.frame = None        # (seq, frame, ts) - the newest decoded frame
        self.fresh = asyncio.Event()
        self.ev_lock = threading.Lock()
        self.st = {"motion_ok": False, "last_frame": None, "last_motion": None}

    async def run(self):
        asyncio.create_task(self._housekeeping())
        while True:
            try:
                await self._read()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("faces: reader crashed")
            self.st["motion_ok"] = False
            if self.ev:
                await self._end()
            await asyncio.sleep(5)

    async def _read(self):
        """Always-on 2 fps decoder: motion runs on every frame, analysis takes the newest frame when it's free."""
        cv2, np = self.eng.cv2, self.eng.np
        motion = Motion(np)
        n = FRAME_W * FRAME_H * 3
        proc = await _ffmpeg(STREAM, f"fps={FPS},scale={FRAME_W}:{FRAME_H}", "bgr24")
        seq = 0
        try:
            while True:
                try:
                    buf = await asyncio.wait_for(proc.stdout.readexactly(n), 20)
                except (asyncio.IncompleteReadError, asyncio.TimeoutError):
                    return
                now = time.time()
                frame = np.frombuffer(buf, np.uint8).reshape(FRAME_H, FRAME_W, 3)
                if corrupt(frame):
                    continue
                seq += 1
                self.st.update(motion_ok=True, last_frame=now)
                small = cv2.cvtColor(cv2.resize(frame, (MW, MH), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
                if motion.feed(small):
                    self.st["last_motion"] = now
                    if self.ev is None:
                        self._start(now)
                    self.ev["last"] = self.ev["moved"] = now
                self.frame = (seq, frame, now)
                self.fresh.set()
                if self.ev and now - self.ev["last"] > MOTION_GAP:
                    await self._end()
        finally:
            await _kill(proc)

    def _start(self, now):
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        eid = self.db.x("INSERT INTO events(day,start,end,fixed) VALUES(?,?,?,1)", (day, now, now))
        self.ev = {"id": eid, "day": day, "start": now, "last": now, "moved": now, "faces": [], "objs": {},
                   "thumb_q": -1.0, "seen": now}
        self.task = asyncio.create_task(self._analyze(self.ev))

    async def _end(self):
        ev, self.ev = self.ev, None
        if self.task:
            self.task.cancel()
            with contextlib.suppress(BaseException):
                await self.task
            self.task = None
        self.db.x("UPDATE events SET end=MAX(end,?) WHERE id=?", (ev["last"], ev["id"]))

    async def _analyze(self, ev):
        done, obj_job, last_obj = 0, None, 0.0
        try:
            while True:
                await self.fresh.wait()
                self.fresh.clear()
                seq, frame, ts = self.frame
                if seq == done:
                    continue
                done = seq
                quiet = ts - ev["seen"] > 30          # nothing found for 30 s (wind, shadows): slow down
                if quiet and seq % 4:
                    continue
                if self.eng.obj is not None and (obj_job is None or obj_job.done()) \
                        and ts - last_obj >= (OBJ_EVERY * (3 if quiet else 1)):
                    last_obj = ts
                    obj_job = asyncio.create_task(asyncio.to_thread(self._objects, ev, frame, ts))
                await asyncio.to_thread(self._faces, ev, frame, ts)
        except asyncio.CancelledError:
            if obj_job:
                with contextlib.suppress(BaseException):
                    await obj_job
            raise
        except Exception:
            log.exception("faces: analysis failed")

    # ---- worker threads
    def _maybe_thumb(self, ev, frame, score):
        with self.ev_lock:
            if score <= ev["thumb_q"]:
                return
            ev["thumb_q"] = score
        _write(ev["day"], f"e{ev['id']}.jpg", self.eng.thumb(frame))
        self.db.x("UPDATE events SET thumb=? WHERE id=?", (f"e{ev['id']}.jpg", ev["id"]))

    def _objects(self, ev, frame, ts):
        try:
            found = self.eng.objects(frame)
        except Exception:
            log.exception("faces: object detection failed")
            return
        if not found:
            return
        ev["seen"] = ts
        if ts - ev["moved"] < STILL_MAX:     # someone standing still keeps the event open
            ev["last"] = max(ev["last"], ts)
        counts = {}
        for o in found:
            counts[o["label"]] = counts.get(o["label"], 0) + 1
        for o in sorted(found, key=lambda o: -o["conf"]):
            lab = o["label"]
            cur = ev["objs"].get(lab)
            area = o["box"][2] * o["box"][3]
            score = o["conf"] * min(1.0, area / 60000)
            if cur and score <= cur["score"]:
                if counts[lab] > cur["n"]:
                    cur["n"] = counts[lab]
                    self.db.x("UPDATE objects SET n=? WHERE id=?", (cur["n"], cur["id"]))
                continue
            jpg = self.eng.crop(frame, *o["box"], 0.08, 320)
            if cur:
                cur.update(score=score, n=max(cur["n"], counts[lab]))
                self.db.x("UPDATE objects SET ts=?, conf=MAX(conf,?), n=? WHERE id=?", (ts, o["conf"], cur["n"], cur["id"]))
                _write(ev["day"], f"o{cur['id']}.jpg", jpg)
            else:
                oid = self.db.x("INSERT INTO objects(event_id,day,ts,label,conf,n,file) VALUES(?,?,?,?,?,?,?)",
                                (ev["id"], ev["day"], ts, lab, o["conf"], counts[lab], None))
                _write(ev["day"], f"o{oid}.jpg", jpg)
                self.db.x("UPDATE objects SET file=? WHERE id=?", (f"o{oid}.jpg", oid))
                ev["objs"][lab] = {"id": oid, "score": score, "n": counts[lab]}
                log.info("faces: %s in event %s (%.2f)", lab, ev["id"], o["conf"])
        self._maybe_thumb(ev, frame, 1 + max(o["conf"] for o in found))

    def _faces(self, ev, frame, ts):
        found = self.eng.faces(frame)
        self._maybe_thumb(ev, frame, (3 + max(f["q"] for f in found)) if found else 0.0)
        for f in found:
            ev["seen"] = ts
            self._add_face(ev, f, ts)
        self.db.x("UPDATE events SET end=MAX(end,?), faces=? WHERE id=?", (ts, len(ev["faces"]), ev["id"]))

    def _add_face(self, ev, f, ts):
        pid, sim = self.eng.match(f["emb"], f["q"])
        rank = f["q"] + 0.001 * f["size"]
        # The person detector never saw more than one person this event: every face is that person.
        single = ev["objs"].get("person", {}).get("n", 0) == 1
        for slot in ev["faces"]:
            if single or float(slot["emb"] @ f["emb"]) > SAME or (pid is not None and slot["pid"] == pid):
                if pid is None:  # keep a recognition an earlier shot already made
                    pid, sim = slot["pid"], slot["sim"]
                if rank > slot["rank"]:  # same person, better shot: replace the crop
                    slot.update(rank=rank, emb=f["emb"], pid=pid, sim=sim)
                    _write(ev["day"], f"f{slot['id']}.jpg", f["jpg"])
                    self.db.x("UPDATE faces SET ts=?,score=?,size=?,quality=?,q=?,yaw=?,sharp=?,emb=?,match_id=?,"
                              "match_sim=? WHERE id=? AND person_id IS NULL",
                              (ts, f["score"], f["size"], rank, f["q"], f["yaw"], f["sharp"], f["emb"].tobytes(),
                               pid, sim, slot["id"]))
                elif pid is not None and slot["pid"] is None:  # worse shot, but this one was recognised
                    slot.update(pid=pid, sim=sim)
                    self.db.x("UPDATE faces SET match_id=?, match_sim=? WHERE id=? AND person_id IS NULL",
                              (pid, sim, slot["id"]))
                return
        if len(ev["faces"]) >= MAX_FACES:
            return
        fid = self.db.x("INSERT INTO faces(event_id,day,ts,score,size,quality,q,yaw,sharp,emb,match_id,match_sim) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ev["id"], ev["day"], ts, f["score"], f["size"], rank, f["q"], f["yaw"], f["sharp"],
                         f["emb"].tobytes(), pid, sim))
        _write(ev["day"], f"f{fid}.jpg", f["jpg"])
        self.db.x("UPDATE faces SET file=? WHERE id=?", (f"f{fid}.jpg", fid))
        ev["faces"].append({"id": fid, "emb": f["emb"], "rank": rank, "pid": pid, "sim": sim})
        log.info("faces: face %s in event %s (q %.2f, %dpx, match %s)", fid, ev["id"], f["q"], f["size"], pid)

    def forget(self, fid: int):
        ev = self.ev
        if ev:
            ev["faces"] = [s for s in ev["faces"] if s["id"] != fid]

    # ---- one-off upgrades of older data + retention
    def _backfill_quality(self):
        """Faces stored before quality scoring: re-detect on the saved crop to get pose and sharpness."""
        cv2 = self.eng.cv2
        rows = self.db.q("SELECT id, day, file FROM faces WHERE q IS NULL")
        for r in rows:
            im = cv2.imread(str(DATA / r["day"] / (r["file"] or "")))
            q = yaw = sharp = 0
            if im is not None:
                self.eng.det.setInputSize((im.shape[1], im.shape[0]))
                _, found = self.eng.det.detect(im)
                if found is not None:
                    f = max(found, key=lambda a: a[2] * a[3])
                    q, yaw, sharp = self.eng.quality(f, self.eng.rec.alignCrop(im, f))
            self.db.x("UPDATE faces SET q=?, yaw=?, sharp=? WHERE id=?", (q, yaw, sharp, r["id"]))
        if rows:
            log.info("faces: scored %d older faces", len(rows))

    def _segment_at(self, t):
        d = datetime.fromtimestamp(t)
        best = None
        for p in sorted((NVR / d.strftime("%Y/%m/%d")).glob("*.mp4")):
            try:
                h, m, s = (int(v) for v in p.stem.split("-"))
            except ValueError:
                continue
            start = d.replace(hour=h, minute=m, second=s, microsecond=0).timestamp()
            if start <= t:
                best = (p, start)
        return best

    def _fix_thumbs(self):
        """Events recorded before the grey-frame filter: take a clean frame from the NAS recording instead."""
        cv2 = self.eng.cv2
        fixed = 0
        for e in self.db.q("SELECT id, day, start, end, thumb FROM events WHERE fixed=0 ORDER BY id"):
            p = DATA / e["day"] / (e["thumb"] or "-")
            im = cv2.imread(str(p)) if e["thumb"] else None
            if im is None or corrupt(im):
                seg = self._segment_at(e["start"] + min(2.0, (e["end"] - e["start"]) / 2))
                if seg:
                    out = DATA / e["day"] / f"e{e['id']}.jpg"
                    tmp = out.with_suffix(".new.jpg")
                    out.parent.mkdir(parents=True, exist_ok=True)
                    tmp.unlink(missing_ok=True)
                    off = max(0.0, e["start"] - seg[1] + min(2.0, (e["end"] - e["start"]) / 2))
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        subprocess.run(["ffmpeg", "-v", "quiet", "-y", "-ss", f"{off:.1f}", "-i", str(seg[0]),
                                        "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "5", str(tmp)],
                                       timeout=30, stdin=subprocess.DEVNULL)
                    new = cv2.imread(str(tmp)) if tmp.exists() else None
                    if new is not None and not corrupt(new):
                        tmp.replace(out)
                        self.db.x("UPDATE events SET thumb=? WHERE id=?", (out.name, e["id"]))
                        fixed += 1
                    tmp.unlink(missing_ok=True)
            self.db.x("UPDATE events SET fixed=1 WHERE id=?", (e["id"],))
        if fixed:
            log.info("faces: replaced %d grey thumbnails from recordings", fixed)

    def _prune(self):
        cut = time.time() - RETENTION_DAYS * 86400
        for e in self.db.q("SELECT id, day, thumb FROM events WHERE start < ?", (cut,)):
            for f in self.db.q("SELECT id, file FROM faces WHERE event_id=? AND person_id IS NULL", (e["id"],)):
                if f["file"]:
                    (DATA / e["day"] / f["file"]).unlink(missing_ok=True)
                self.db.x("DELETE FROM faces WHERE id=?", (f["id"],))
            for o in self.db.q("SELECT id, file FROM objects WHERE event_id=?", (e["id"],)):
                if o["file"]:
                    (DATA / e["day"] / o["file"]).unlink(missing_ok=True)
                self.db.x("DELETE FROM objects WHERE id=?", (o["id"],))
            if not self.db.q("SELECT 1 FROM faces WHERE event_id=? LIMIT 1", (e["id"],)):
                if e["thumb"]:
                    (DATA / e["day"] / e["thumb"]).unlink(missing_ok=True)
                self.db.x("DELETE FROM events WHERE id=?", (e["id"],))
                with contextlib.suppress(OSError):
                    (DATA / e["day"]).rmdir()

    async def _housekeeping(self):
        for job in (self._backfill_quality, lambda: self.eng.relearn(self.db), self._fix_thumbs):
            try:
                await asyncio.to_thread(job)
            except Exception:
                log.exception("faces: upgrade step failed")
        while True:
            try:
                await asyncio.to_thread(self._prune)
            except Exception:
                log.exception("faces: prune failed")
            await asyncio.sleep(3600)


# ---------------------------------------------------------------- API
router = APIRouter()
W: Watcher | None = None
REASON = "starting"


async def start():
    global W, REASON
    if os.getenv("FACES_ENABLED", "1") != "1":
        REASON = "turned off (FACES_ENABLED=0)"
        return
    if not (YUNET.exists() and SFACE.exists()):
        REASON = f"models missing in {MODELS}"
        log.warning("faces: %s", REASON)
        return
    try:
        eng = Engine()
        DATA.mkdir(parents=True, exist_ok=True)
        db = DB(DATA / "faces.db")
        await asyncio.to_thread(eng.relearn, db)
    except Exception as e:
        REASON = f"could not start: {e}"
        log.exception("faces: start failed")
        return
    REASON = None
    W = Watcher(db, eng)
    W.loop = asyncio.create_task(W.run())
    log.info("faces: watching %s (objects %s)", STREAM, "on" if eng.obj is not None else "off")


def _w() -> Watcher:
    if not W:
        raise HTTPException(503, f"detection is off: {REASON}")
    return W


def status() -> dict:
    if not W:
        return {"enabled": False, "reason": REASON}
    st = dict(W.st)
    st.update(enabled=True, active=W.ev is not None, objects=W.eng.obj is not None,
              motion_ok=st["motion_ok"] and time.time() - (st["last_frame"] or 0) < 20)
    return st


def _img(day, file):
    return f"/api/faces/img/{day}/{file}" if file else None


def _face(r) -> dict:
    confirmed = r["person_id"] is not None
    return {"id": r["id"], "event": r["event_id"], "ts": r["ts"], "size": r["size"],
            "q": round(r["q"], 2) if r["q"] is not None else None, "img": _img(r["day"], r["file"]),
            "name": r["pname"] if confirmed else r["mname"], "person": r["person_id"] or r["match_id"],
            "confirmed": confirmed, "sim": None if confirmed or r["match_sim"] is None else round(r["match_sim"], 2)}


FACE_SQL = ("SELECT f.*, p.name pname, m.name mname FROM faces f LEFT JOIN people p ON p.id=f.person_id "
            "LEFT JOIN people m ON m.id=f.match_id ")


def _relearn_cleanup(w: Watcher):
    w.db.x("DELETE FROM people WHERE id NOT IN (SELECT person_id FROM faces WHERE person_id IS NOT NULL)")
    w.eng.relearn(w.db)


def _person_id(w: Watcher, name: str):
    name = re.sub(r"\s+", " ", name).strip()[:40]
    if not name:
        return None
    w.db.x("INSERT OR IGNORE INTO people(name, created) VALUES(?, ?)", (name, time.time()))
    return w.db.q("SELECT id FROM people WHERE name=?", (name,))[0]["id"]


@router.get("/api/faces/status")
async def api_status():
    return status()


@router.get("/api/activity")
async def api_activity(day: str):
    """Every motion event of a day with its faces, people and animals."""
    w = _w()
    if not DAY_RE.match(day):
        raise HTTPException(400, "bad day")

    def load():
        evs = w.db.q("SELECT * FROM events WHERE day=? ORDER BY start", (day,))
        faces = w.db.q(FACE_SQL + "WHERE f.day=? AND f.file IS NOT NULL ORDER BY f.q DESC", (day,))
        objs = w.db.q("SELECT * FROM objects WHERE day=? AND file IS NOT NULL ORDER BY conf DESC", (day,))
        return evs, faces, objs

    evs, faces, objs = await asyncio.to_thread(load)
    fby, oby = {}, {}
    for r in faces:
        fby.setdefault(r["event_id"], []).append(_face(r))
    for r in objs:
        oby.setdefault(r["event_id"], []).append({"id": r["id"], "label": r["label"], "conf": round(r["conf"], 2),
                                                  "n": r["n"], "img": _img(day, r["file"])})
    live = W.ev if W else None
    out = []
    for e in evs:
        fl = fby.get(e["id"], [])
        people, seen = [], set()
        for f in fl:
            if f["person"] and f["person"] not in seen:
                seen.add(f["person"])
                people.append({"id": f["person"], "name": f["name"], "confirmed": f["confirmed"], "face": f["id"]})
        is_live = live is not None and live["id"] == e["id"]
        out.append({"id": e["id"], "start": e["start"], "end": max(e["end"], live["last"]) if is_live else e["end"],
                    "live": is_live, "thumb": _img(day, e["thumb"]), "faces": fl, "objects": oby.get(e["id"], []),
                    "people": people, "unknown": sum(1 for f in fl if not f["person"])})
    return out


@router.get("/api/faces/img/{day}/{name}")
async def api_img(day: str, name: str):
    if not DAY_RE.match(day) or not FILE_RE.match(name):
        raise HTTPException(400, "bad path")
    p = DATA / day / name
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=60"})


def _cover(w: Watcher, p) -> str | None:
    rows = []
    if p["cover"]:
        rows = w.db.q("SELECT day, file FROM faces WHERE id=? AND person_id=?", (p["cover"], p["id"]))
    if not rows:
        rows = w.db.q("SELECT day, file FROM faces WHERE person_id=? ORDER BY q DESC, size DESC LIMIT 1", (p["id"],))
    return _img(rows[0]["day"], rows[0]["file"]) if rows else None


@router.get("/api/people")
async def api_people():
    w = _w()
    cut = time.time() - 30 * 86400

    def load():
        out = []
        for p in w.db.q("SELECT * FROM people ORDER BY name"):
            s = w.db.q("""SELECT COUNT(*) FILTER (WHERE person_id=:p) samples,
                                 COUNT(*) FILTER (WHERE person_id IS NULL AND match_id=:p) guesses,
                                 MAX(ts) last, COUNT(DISTINCT event_id) FILTER (WHERE ts > :cut) sightings
                          FROM faces WHERE person_id=:p OR match_id=:p""", {"p": p["id"], "cut": cut})[0]
            out.append({"id": p["id"], "name": p["name"], "img": _cover(w, p), "samples": s["samples"],
                        "guesses": s["guesses"], "last_seen": s["last"], "sightings": s["sightings"]})
        return out
    return await asyncio.to_thread(load)


@router.get("/api/people/{pid}")
async def api_person(pid: int):
    """Profile: samples, suggested matches to review, sightings and when they're usually around."""
    w = _w()

    def load():
        p = w.db.q("SELECT * FROM people WHERE id=?", (pid,))
        if not p:
            raise HTTPException(404)
        p = p[0]
        samples = [_face(r) for r in w.db.q(FACE_SQL + "WHERE f.person_id=? ORDER BY f.q DESC, f.ts DESC", (pid,))]
        review = [_face(r) for r in w.db.q(FACE_SQL + "WHERE f.person_id IS NULL AND f.match_id=? "
                                           "ORDER BY f.ts DESC LIMIT 80", (pid,))]
        rows = w.db.q("""SELECT e.id, e.day, e.start, e.end, e.thumb, MAX(f.q) q,
                                (SELECT g.file FROM faces g WHERE g.event_id=e.id AND (g.person_id=:p OR
                                   (g.person_id IS NULL AND g.match_id=:p)) ORDER BY g.q DESC LIMIT 1) face
                         FROM events e JOIN faces f ON f.event_id=e.id
                         WHERE f.person_id=:p OR (f.person_id IS NULL AND f.match_id=:p)
                         GROUP BY e.id ORDER BY e.start DESC""", {"p": pid})
        hours, days = [0] * 24, {}
        cut = time.time() - 30 * 86400
        for r in rows:
            if r["start"] > cut:
                hours[datetime.fromtimestamp(r["start"]).hour] += 1
                days[r["day"]] = days.get(r["day"], 0) + 1
        sightings = [{"event": r["id"], "day": r["day"], "start": r["start"], "end": r["end"],
                      "thumb": _img(r["day"], r["thumb"]), "face": _img(r["day"], r["face"])} for r in rows[:60]]
        return {"id": p["id"], "name": p["name"], "img": _cover(w, p), "cover": p["cover"], "created": p["created"],
                "samples": samples, "review": review, "sightings": sightings, "total_sightings": len(rows),
                "last_seen": rows[0]["start"] if rows else None, "hours": hours, "days": days}
    return await asyncio.to_thread(load)


@router.post("/api/people/{pid}")
async def api_person_update(pid: int, request: Request):
    """Rename (an existing name merges the two people) and/or pick the profile photo."""
    w = _w()
    b = await request.json()
    if not w.db.q("SELECT 1 FROM people WHERE id=?", (pid,)):
        raise HTTPException(404)
    if "cover" in b:
        fid = int(b["cover"])
        if not w.db.q("SELECT 1 FROM faces WHERE id=? AND person_id=?", (fid, pid)):
            raise HTTPException(400, "that face is not a sample of this person")
        w.db.x("UPDATE people SET cover=? WHERE id=?", (fid, pid))
    if "name" in b:
        name = re.sub(r"\s+", " ", str(b["name"])).strip()[:40]
        if not name:
            raise HTTPException(400, "name required")
        other = w.db.q("SELECT id FROM people WHERE name=? AND id<>?", (name, pid))
        if other:  # merge into the existing person
            oid = other[0]["id"]
            w.db.x("UPDATE faces SET person_id=? WHERE person_id=?", (oid, pid))
            w.db.x("DELETE FROM people WHERE id=?", (pid,))
            await asyncio.to_thread(_relearn_cleanup, w)
            return {"ok": True, "id": oid, "merged": True}
        w.db.x("UPDATE people SET name=? WHERE id=?", (name, pid))
    return {"ok": True, "id": pid}


@router.delete("/api/people/{pid}")
async def api_delete_person(pid: int):
    """Forget a person: their faces stay, but become unnamed."""
    w = _w()
    w.db.x("UPDATE faces SET person_id=NULL WHERE person_id=?", (pid,))
    w.db.x("DELETE FROM people WHERE id=?", (pid,))
    await asyncio.to_thread(w.eng.relearn, w.db)
    return {"ok": True}


def _ids(b) -> list:
    ids = [int(v) for v in b.get("ids", [])][:500]
    if not ids:
        raise HTTPException(400, "no faces given")
    return ids


@router.post("/api/faces/{fid}/label")
async def api_label(fid: int, request: Request):
    """Name a face (it becomes a sample for that person), or clear the name with an empty string."""
    return await _label(_w(), [fid], str((await request.json()).get("name", "")))


@router.post("/api/faces/label")
async def api_label_many(request: Request):
    b = await request.json()
    return await _label(_w(), _ids(b), str(b.get("name", "")))


async def _label(w: Watcher, ids: list, name: str):
    pid = _person_id(w, name)
    marks = ",".join("?" * len(ids))
    w.db.x(f"UPDATE faces SET person_id=?, match_id=NULL, match_sim=NULL, reject='' WHERE id IN ({marks})", (pid, *ids))
    await asyncio.to_thread(_relearn_cleanup, w)
    return {"ok": True, "person": pid}


@router.post("/api/faces/{fid}/reject")
async def api_reject(fid: int):
    """'That's not them': stop suggesting the current match for this face."""
    w = _w()
    r = w.db.q("SELECT match_id, reject FROM faces WHERE id=?", (fid,))
    if not r:
        raise HTTPException(404)
    if r[0]["match_id"]:
        rej = ",".join(sorted({*filter(None, (r[0]["reject"] or "").split(",")), str(r[0]["match_id"])}))
        w.db.x("UPDATE faces SET reject=?, match_id=NULL, match_sim=NULL WHERE id=?", (rej, fid))
        await asyncio.to_thread(w.eng.relearn, w.db)
    return {"ok": True}


@router.post("/api/faces/delete")
async def api_delete_many(request: Request):
    w = _w()
    for fid in _ids(await request.json()):
        await _delete_face(w, fid)
    await asyncio.to_thread(_relearn_cleanup, w)
    return {"ok": True}


@router.delete("/api/faces/{fid}")
async def api_delete_face(fid: int):
    """Not a face / not wanted: remove it (and its sample, if it was named)."""
    w = _w()
    labelled = await _delete_face(w, fid)
    if labelled:
        await asyncio.to_thread(_relearn_cleanup, w)
    return {"ok": True}


async def _delete_face(w: Watcher, fid: int) -> bool:
    r = w.db.q("SELECT event_id, day, file, person_id FROM faces WHERE id=?", (fid,))
    if not r:
        return False
    r = r[0]
    w.forget(fid)
    w.db.x("DELETE FROM faces WHERE id=?", (fid,))
    if r["file"]:
        (DATA / r["day"] / r["file"]).unlink(missing_ok=True)
    w.db.x("UPDATE events SET faces=(SELECT COUNT(*) FROM faces WHERE event_id=?) WHERE id=?",
           (r["event_id"], r["event_id"]))
    return r["person_id"] is not None


@router.get("/api/faces/unknown")
async def api_unknown():
    """Unnamed, unmatched faces from the last 30 days, grouped by likeness so a whole group can be named at once."""
    w = _w()
    np = w.eng.np

    def load():
        rows = w.db.q(FACE_SQL + "WHERE f.person_id IS NULL AND f.match_id IS NULL AND f.file IS NOT NULL "
                      "AND f.ts > ? ORDER BY f.q DESC", (time.time() - 30 * 86400,))
        groups = []  # [centroid, [rows]]
        for r in rows:
            e = np.frombuffer(r["emb"], np.float32)
            best, gi = 0.0, -1
            for i, g in enumerate(groups):
                s = float(g[0] @ e) / (float(np.linalg.norm(g[0])) + 1e-9)
                if s > best:
                    best, gi = s, i
            if gi >= 0 and best >= SAME:
                groups[gi][0] = groups[gi][0] + e
                groups[gi][1].append(r)
            elif (r["q"] or 0) >= 0.15 or len(groups) < 200:
                groups.append([e.copy(), [r]])
        groups.sort(key=lambda g: (-len(g[1]), -max(x["q"] or 0 for x in g[1])))
        return [{"ids": [x["id"] for x in g[1]], "faces": [_face(x) for x in g[1][:14]], "count": len(g[1]),
                 "last": max(x["ts"] for x in g[1])} for g in groups[:24]]
    return await asyncio.to_thread(load)
