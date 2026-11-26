# SentinelNVR design

Target: a small Proxmox LXC (Debian 12, 2 vCPU, 4 GB RAM, 20 GB disk) on modest old hardware, with recordings on a NAS (SMB/NFS).
Nothing here is Proxmox-specific; any Debian/Ubuntu machine with ffmpeg works.

## Components
1. **go2rtc**: restreams the camera's RTSP main + sub streams. Live WebRTC/MSE/HLS. H.264 transcode only on demand (low-power CPUs).
2. **recorder** (`deploy/recorder.sh`): ffmpeg `-c copy` (no transcode) into 2-minute fMP4 segments, `YYYY/MM/DD/HH-MM-SS.mp4`
   on the NAS. Watchdog + auto-restart; audio kept.
3. **retention** (`deploy/retention.sh`): nightly job deletes segments older than N days; refuses to run if the NAS is not mounted.
4. **API** (`app/server.py`, FastAPI): login, PTZ (ONVIF SOAP), snapshots, segment index/timeline, clip export.
5. **Detection** (`app/faces.py`): one always-on ffmpeg decodes the main stream at 2 fps -> 160x90 frame-diff motion.
   During an event: YOLOX-s (people/animals, every ~1.5 s) and YuNet + SFace (faces) run in worker threads.
   Faces get a quality score (pose x sharpness x size); recognition = top-2 mean cosine to a person's good samples,
   >= 0.38 and 0.05 ahead of the next person.
6. **PWA** (`app/static`, vanilla JS): live view (main/sub), PTZ pad + zoom, timeline scrubber, clip download, snapshot,
   activity + people tabs, installable (manifest + service worker). HTTPS via `tailscale serve` or a reverse proxy.

## Decisions
- Copy-only recording keeps CPU near idle; the container disk holds only the OS + app, all video lives on the NAS.
- The camera has no auth: only the container may reach it (firewall/VLAN); the app provides the login.
- Frigate was rejected as heavier than a 2-core box wants; detection is done in-app and only while there is motion.
- Detection cost on a 2012 laptop-class i5: idle ~15% of one core, ~1-1.3 cores during an event, ~230 MB RSS.
