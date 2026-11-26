# SentinelNVR

**A self-hosted, ad-free home for your cheap Wi-Fi camera.**

## Why this project exists

It started with a cheap Chinese Wi-Fi PTZ camera bought online. The hardware was fine: 1080p, pan/tilt, night vision.
The software was not. Watching your own camera meant installing a vendor app full of ads, routing your video through
a cloud server on the other side of the world, and having no way to save recordings or plug the camera into anything else.

It turned out the camera speaks standard protocols (RTSP for video, ONVIF for pan/tilt) with no password, so the
vendor app wasn't needed at all. SentinelNVR is what grew out of that: a small, private, self-hostable app that

- shows the **live view** in your browser or as an installable phone app (PWA),
- **records 24/7** to your NAS or disk and keeps as much history as you want,
- lets you **pan/tilt/zoom** the camera,
- finds **motion, people, animals and faces** (and learns who is who) on a normal CPU, with no cloud and no GPU,
- runs happily on a tiny homelab box (a 2-core Proxmox container is enough),
- and has **no ads, no accounts, no telemetry**. Your video never leaves your network unless you decide it should.

If you have a camera that offers RTSP and ONVIF (most cheap ones do), this will probably work for you too.

## What you get

| Feature | Notes |
|---|---|
| Live view | Low-latency in the browser; main and sub streams; H.264 transcode only when a browser needs it |
| Continuous recording | `ffmpeg -c copy` (no re-encoding, so almost no CPU) into 2-minute segments |
| Timeline | Scrub through any day, download clips, grab snapshots |
| PTZ | Pan/tilt/zoom pad (ONVIF) |
| Activity | Motion, people, animals and faces per day, filterable |
| People | Name a face once and it recognises that person from then on |
| Retention | Automatically deletes recordings older than N days (default 30) |
| Login | Password-protected, rate-limited, secure cookies behind HTTPS |

## What you need

- A camera with **RTSP** (and ideally **ONVIF** for PTZ). It should be reachable on your LAN.
- A small always-on Linux machine: a **Debian 12/13** VM or LXC container, 2 vCPU / 4 GB RAM is plenty.
- Somewhere to store video: roughly **4 GB per day** for a 1080p H.265 camera (about 130 GB for 30 days). A NAS share is ideal.
- Basic comfort with a terminal.

## Quick start (about 10 minutes)

**1. Find your camera's stream.** Try `rtsp://<camera-ip>:554/` in VLC. If it asks for nothing and shows video, great.
Many cameras use paths like `/0/av0` (main) and `/0/av1` (sub). Edit `deploy/go2rtc.yaml` if yours differ,
and add a username/password to the URL if your camera needs one (`rtsp://user:pass@ip:554/path`).
See [NOTES.md](NOTES.md) for how one real camera was probed.

**2. Give the machine storage.** Mount your NAS (or a disk) at `/mnt/nvr`. See [NAS storage](#nas-storage) below.

**3. Install.** On the Debian machine, as root:

```bash
apt install -y git
git clone https://github.com/tanislavivanov/SentinelNVR.git
cd SentinelNVR
CAMERA_IP=192.168.1.50 TZONE=Europe/London bash deploy/install.sh
```

Use your camera's real IP and your own [timezone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)
(recordings are named by local time). The installer prints a **randomly generated web password once**. Save it. It is
also stored in `/etc/homecam/env`.

**4. Open it.** Go to `http://<machine-ip>:8080` and sign in.

**5. (Optional) Install it as an app on your phone.** Phones require HTTPS for that. The easiest way is
[Tailscale](https://tailscale.com): `tailscale serve --bg 8080`, then set `COOKIE_SECURE=1` in `/etc/homecam/env` and run
`systemctl restart homecam-api`. A reverse proxy such as Caddy or Nginx Proxy Manager works too; set `FORWARDED_ALLOW_IPS`
in the env file to the proxy's IP so login rate limiting sees real client addresses.

> The internal service and folder names still say `homecam` (e.g. `/etc/homecam`, `systemctl status homecam-api`). That is expected.

## NAS storage

Do this on the Proxmox **host** (not inside the container). The NAS is mounted on the host and bind-mounted into the
LXC, so the container needs no special privileges. SMB with uid mapping suits an unprivileged LXC (container root = host uid 100000):

```bash
apt install -y cifs-utils
mkdir -p /mnt/nas-nvr
printf 'username=NASUSER\npassword=NASPASS\n' > /root/.nascreds && chmod 600 /root/.nascreds
echo '//NAS_IP/SHARE/NVR /mnt/nas-nvr cifs credentials=/root/.nascreds,uid=100000,gid=100000,file_mode=0666,dir_mode=0777,_netdev,nofail,vers=2.0 0 0' >> /etc/fstab
systemctl daemon-reload && mount /mnt/nas-nvr && touch /mnt/nas-nvr/test && rm /mnt/nas-nvr/test
pct set <CTID> -mp0 /mnt/nas-nvr,mp=/mnt/nvr && pct reboot <CTID>
```

Replace `NASUSER`, `NASPASS`, `NAS_IP` and `SHARE` with your own. If your NAS refuses SMB v2, try `vers=1.0` or `3.0`.
The share must be writable by that user. Not using Proxmox? Just mount any disk or share at `/mnt/nvr`.

After a host reboot the NAS mount can lose the race with the network. A small hookscript makes the container wait up to
3 minutes for the share (and reboot it once the share appears if the NAS is slow):

```bash
cp deploy/proxmox/homecam-nas-wait.sh /var/lib/vz/snippets/ && chmod +x /var/lib/vz/snippets/homecam-nas-wait.sh
pct set <CTID> -hookscript local:snippets/homecam-nas-wait.sh
```

It needs the `.homecam` marker file on the share, which `install.sh` creates. Logs: `journalctl -t homecam-nas-wait`.

## Detection: people, animals, faces

Runs inside the API service, CPU only. One ffmpeg decodes the main stream at 2 fps all the time (about 15% of one core),
so there is no start-up delay when something moves. Motion opens an event; while it lasts, YOLOX-s finds people and
animals (dog, cat, bird, horse, sheep, cow, bear) about every 1.5 s, and YuNet/SFace look for faces on every frame
(about one core while busy). A person standing still keeps the event open for up to 2 minutes.

- **Activity tab**: events per day, filter All / People / Animals / Unknown / Motion. Tap a face to name it,
  "Not <name>" to reject a suggestion, "Not a face" to delete.
- **People tab**: a profile per person (visits by hour, suggested matches, photos). "Who's this?" groups unnamed
  faces by likeness so you can name a whole group at once. Renaming to an existing name merges them.
- Recognition only learns from good samples (frontal, sharp, bigger than ~80 px). Animals are detected by species only.
- Models are downloaded by `install.sh` (OpenCV Zoo) into `/opt/homecam/models`. Data lives in `/var/lib/homecam/faces`.
- Unnamed faces, animals and motion events follow `RETENTION_DAYS`; named faces are kept as recognition samples.

Tuning goes in `/etc/homecam/env`: `FACES_ENABLED=0` (turn everything off), `OBJECTS_ENABLED=0` (no people/animals),
`MOTION_AREA` (default 0.006; raise it if wind or trees trigger events), `MOTION_LEVEL` (25), `FACE_MIN_SCORE` (0.80),
`FACE_MIN_SIZE` (36 px), `FACE_MATCH` (0.38), `FACE_MARGIN` (0.05), `OBJECT_INTERVAL` (1.5 s), `FACE_DETECT_WIDTH` (1280).
Logs: `journalctl -u homecam-api | grep faces`.

## Day-to-day

```bash
systemctl status go2rtc homecam-api homecam-recorder
journalctl -u homecam-recorder -f
systemctl list-timers homecam-retention.timer
```

Change retention or segment length in `/etc/homecam/env`, then `systemctl restart homecam-api homecam-recorder`.

## Security: please read

- **Most of these cameras have no authentication.** Anyone who can reach the camera can watch and steer it.
  Put it on an isolated VLAN or firewall it so **only the SentinelNVR machine can reach it**, and block it from the
  internet so it stops phoning home to the vendor's cloud.
- Do not expose port 8080 directly to the internet. Use a VPN (Tailscale/WireGuard) or an HTTPS reverse proxy.
- Use a strong password. The installer generates one; change `HOMECAM_PASSWORD` in `/etc/homecam/env` if you like.
- Never commit your `/etc/homecam/env` file or NAS credentials anywhere.

## Known limits

- Camera SD-card recordings can't be read over the LAN on the camera this was built with; the NVR replaces them.
- PTZ presets: the tested camera did not answer `GetPresets`. Move/stop work.
- The recorder uses `-tag:v hvc1` for H.265. If your camera is H.264, remove that flag in `deploy/recorder.sh`.
- The installer targets x86_64 Debian/Ubuntu. ARM would need a different go2rtc build.

## Project layout

- `app/` FastAPI backend + static PWA (login, live, PTZ, timeline, export, detection)
- `deploy/` `install.sh`, go2rtc config, `recorder.sh`, `retention.sh`, systemd units, Proxmox hookscript
- `NOTES.md` findings from probing the original camera, `DESIGN.md` architecture

## What's next / Timeline

SentinelNVR is actively developed and still young. This is the rough plan. It is a direction, not a promise, and
priorities will shift based on what people actually need.

**Now: getting it ready for everyone**
- Open-source release, cleaned of personal setup details
- Beginner-friendly docs and a clearer install guide
- License chosen (PolyForm Noncommercial)

**Next: easier setup**
- One-command install with a guided first-run wizard (camera IP, password, storage, timezone)
- Camera auto-discovery (ONVIF) and a built-in "test my camera" check
- Docker / Docker Compose image, so Proxmox and Debian are no longer required
- ARM support (Raspberry Pi and similar)
- Rename the internal `homecam` service names to `sentinelnvr`

**Later: new features**
- Multiple cameras in one dashboard
- Push notifications (people, animals, unknown faces)
- Home Assistant and MQTT integration
- Configurable motion zones and schedules
- Optional hardware acceleration for detection
- Multiple user accounts

**Always: quality**
- Performance work (lower CPU and memory, faster timeline and thumbnails)
- Bug fixes and hardening, especially around recorder reliability and security
- Tests and a proper CI pipeline
- Support for more camera brands and models

## Contributing: you are very welcome here

Contributions of every size are wanted and appreciated. You don't need to be an expert.

- **Open an issue** for bugs, questions, feature ideas, or "this is confusing in the README". All of it helps.
- **Open a pull request** for fixes, features, docs or translations. For anything big, opening an issue first to
  talk it through is a nice way to avoid wasted work, but small PRs can just be sent.
- **Report your camera**: tell us the brand/model and whether it worked. That helps everyone with the same camera.
- **Share feedback** on the setup experience. Where you got stuck is exactly what gets improved next.

Please never post real passwords, public IPs or camera footage in issues.

## License

SentinelNVR is released under the [PolyForm Noncommercial License 1.0.0](LICENSE).

- **Free to use, modify and fork** for personal, hobby, research, educational and other noncommercial purposes.
- **Not allowed:** selling it, offering it as a paid product or service, or using it for commercial gain.
  Want to use it commercially? Open an issue to talk about a separate license.
- **Attribution required:** if you share it or a fork, keep the `LICENSE` file and the `Required Notice:` line with the
  link to this repository: <https://github.com/tanislavivanov/SentinelNVR>.
- **No warranty, no liability:** the software is provided "as is". See the license for details.

Note that this is a "source-available" license, not an OSI-approved open-source license.
