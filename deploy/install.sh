#!/bin/bash
# Install SentinelNVR in a Debian 12 container. Run as root from the repo root:  bash deploy/install.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
[ "$(uname -m)" = x86_64 ] || { echo "this installer fetches the amd64 go2rtc build"; exit 1; }
: "${CAMERA_IP:?set your camera IP, e.g.  CAMERA_IP=192.168.1.50 bash deploy/install.sh}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/homecam
ENVF=/etc/homecam/env

# Recordings are named by local time, so the container must share the camera's timezone (LXC defaults to UTC).
timedatectl set-timezone "${TZONE:-UTC}" 2>/dev/null || ln -sf "/usr/share/zoneinfo/${TZONE:-UTC}" /etc/localtime

apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ffmpeg python3 python3-venv curl ca-certificates tzdata

id homecam >/dev/null 2>&1 || useradd --system --home-dir "$APP" --shell /usr/sbin/nologin homecam
mkdir -p "$APP" /etc/homecam /mnt/nvr

if ! command -v go2rtc >/dev/null; then
  curl -fsSL -o /usr/local/bin/go2rtc https://github.com/AlexxIT/go2rtc/releases/latest/download/go2rtc_linux_amd64
  chmod +x /usr/local/bin/go2rtc
fi

rm -rf "$APP/app"; cp -r "$SRC/app" "$APP/app"
install -m 755 "$SRC/deploy/recorder.sh" "$SRC/deploy/retention.sh" "$APP/"
sed "s/192\.168\.1\.50/$CAMERA_IP/g" "$SRC/deploy/go2rtc.yaml" > /etc/homecam/go2rtc.yaml
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q -r "$APP/app/requirements.txt"

# Detection models (OpenCV Zoo): YuNet faces ~230 KB, SFace recognition ~37 MB, YOLOX-s people/animals ~36 MB. Kept across reinstalls.
mkdir -p "$APP/models"
ZOO=https://github.com/opencv/opencv_zoo/raw/main/models
for m in face_detection_yunet/face_detection_yunet_2023mar.onnx face_recognition_sface/face_recognition_sface_2021dec.onnx object_detection_yolox/object_detection_yolox_2022nov.onnx; do
  f="$APP/models/$(basename "$m")"
  [ -s "$f" ] || curl -fsSL -o "$f" "$ZOO/$m" || echo "!! could not download $m - face detection stays off"
done

if [ ! -f "$ENVF" ]; then
  PW="$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 16)"
  cat > "$ENVF" <<EOF
CAMERA_IP=$CAMERA_IP
NVR_DIR=/mnt/nvr
SEGMENT_SECONDS=120
RETENTION_DAYS=30
RECORD_STREAM=homecam
PTZ_PROFILE=Profile_1
GO2RTC_URL=http://127.0.0.1:1984
COOKIE_SECURE=0
# Reverse proxy whose X-Forwarded-For uvicorn trusts, so login rate limiting sees real clients
FORWARDED_ALLOW_IPS=${FORWARDED_ALLOW_IPS:-127.0.0.1}
HOMECAM_PASSWORD=$PW
HOMECAM_SECRET=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')
EOF
  chmod 640 "$ENVF"; chgrp homecam "$ENVF"
  echo "=== SentinelNVR web password: $PW   (stored in $ENVF) ==="
fi
chown -R homecam:homecam "$APP"

for u in go2rtc homecam-api homecam-recorder homecam-retention; do cp "$SRC/deploy/systemd/$u".* /etc/systemd/system/; done
cp "$SRC/deploy/systemd/homecam-retention.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now go2rtc homecam-api homecam-retention.timer

if mountpoint -q /mnt/nvr; then
  touch /mnt/nvr/.homecam && echo "NAS mount OK, marker created"
  systemctl enable --now homecam-recorder
else
  echo "!! /mnt/nvr is not a mount - recorder NOT started. Mount the NAS, then run:"
  echo "   touch /mnt/nvr/.homecam && systemctl enable --now homecam-recorder"
fi
echo "Done. Open http://$(hostname -I | awk '{print $1}'):8080"
