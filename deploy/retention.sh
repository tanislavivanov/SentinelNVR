#!/bin/bash
# Delete recordings older than RETENTION_DAYS. Refuses to run unless the NAS marker file exists.
set -euo pipefail
: "${NVR_DIR:=/mnt/nvr}" "${RETENTION_DAYS:=30}"
[ -f "$NVR_DIR/.homecam" ] || { echo "NVR marker missing - not deleting anything" >&2; exit 1; }
find "$NVR_DIR" -type f -name '*.mp4' -mmin +"$((RETENTION_DAYS * 1440))" -delete
find "$NVR_DIR" -mindepth 1 -type d -empty -delete
# thumbnails live on local disk (CacheDirectory of homecam-api)
[ -d /var/cache/homecam/thumbs ] && find /var/cache/homecam/thumbs -mindepth 1 -mtime +"$RETENTION_DAYS" -delete || true
echo "retention done ($(date))"
