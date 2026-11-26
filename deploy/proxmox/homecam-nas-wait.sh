#!/bin/bash
# Proxmox hookscript for the SentinelNVR LXC (runs on the HOST, not in the container).
# pre-start: wait for the NAS share to be mounted before the container bind-mounts it.
# A mount made on the host after the container starts does not reach the container, so without
# this a slow NAS at boot leaves the recorder looking at an empty folder until someone reboots the CT.
# If the NAS stays down past the wait, the container starts anyway (live view keeps working, the
# recorder refuses to write without the marker) and a background watcher reboots the container
# as soon as the share mounts.
#
# Install:  cp deploy/proxmox/homecam-nas-wait.sh /var/lib/vz/snippets/ && chmod +x /var/lib/vz/snippets/homecam-nas-wait.sh
#           pct set <CTID> -hookscript local:snippets/homecam-nas-wait.sh
set -u
VMID="$1" PHASE="$2"
: "${NAS_MNT:=/mnt/nas-nvr}" "${WAIT_SECONDS:=180}" "${WATCH_SECONDS:=21600}"
MARKER="$NAS_MNT/.homecam"
LOCK="/run/homecam-nas-watch-$VMID.pid"
log() { echo "homecam-nas-wait[$VMID]: $*"; logger -t homecam-nas-wait "[$VMID] $*" 2>/dev/null || true; }

nas_ready() {
  mountpoint -q "$NAS_MNT" || mount "$NAS_MNT" >/dev/null 2>&1 || return 1
  [ -f "$MARKER" ]
}

[ "$PHASE" = pre-start ] || exit 0

deadline=$(( $(date +%s) + WAIT_SECONDS ))
until nas_ready; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    log "NAS not mounted at $NAS_MNT after ${WAIT_SECONDS}s - starting without it; will reboot CT once it mounts"
    if ! { [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK")" 2>/dev/null; }; then
      setsid bash -c '
        echo $$ > "$4"; trap "rm -f \"$4\"" EXIT
        end=$(( $(date +%s) + $5 ))
        while [ "$(date +%s)" -lt "$end" ]; do
          sleep 30
          pct status "$1" | grep -q running || exit 0   # CT stopped by someone: nothing to fix
          if { mountpoint -q "$2" || mount "$2" >/dev/null 2>&1; } && [ -f "$3" ]; then
            logger -t homecam-nas-wait "[$1] NAS is back - rebooting CT to pick up the mount"
            pct reboot "$1"; exit 0
          fi
        done
        logger -t homecam-nas-wait "[$1] gave up waiting for NAS"
      ' _ "$VMID" "$NAS_MNT" "$MARKER" "$LOCK" "$WATCH_SECONDS" </dev/null >/dev/null 2>&1 &
    fi
    exit 0
  fi
  log "waiting for NAS at $NAS_MNT ..."
  sleep 10
done
log "NAS mounted, marker present"
exit 0
