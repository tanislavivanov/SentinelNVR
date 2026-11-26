#!/bin/bash
# Continuous recorder: copies the camera stream (via go2rtc) into NAS segments, no video transcoding.
set -euo pipefail
: "${NVR_DIR:=/mnt/nvr}" "${SEGMENT_SECONDS:=120}" "${RECORD_STREAM:=homecam}"
# Restart ffmpeg if it has written nothing for this long (it can keep reading a broken stream
# while writing nothing, then dump everything into one huge segment named after the time it resumed).
: "${STALL_SECONDS:=90}"
URL="rtsp://127.0.0.1:8554/${RECORD_STREAM}"
[ -f "$NVR_DIR/.homecam" ] || { echo "NVR marker $NVR_DIR/.homecam missing - NAS not mounted? refusing to record" >&2; exit 1; }
# ffmpeg's -strftime_mkdir did not create the nested date dirs on 7.1, so keep today's + tomorrow's ready ourselves.
mkdirs() { for off in 0 1; do mkdir -p "$NVR_DIR/$(date -d "+$off day" +%Y/%m/%d)"; done; }
mkdirs
( while sleep 300; do mkdirs; done ) &
mk=$!

# The camera sometimes stops sending audio for hours while its SDP still lists the track. ffmpeg writes
# nothing until the AAC encoder has seen a first audio frame, so recording "with audio" then records
# nothing at all. Only include audio when packets are actually arriving.
has_audio() {
  timeout 12 ffprobe -v quiet -rtsp_transport tcp -i "$URL" -select_streams a:0 -read_intervals %+6 \
    -show_entries packet=pts -of csv=p=0 2>/dev/null | grep -q '[0-9]'
}
if has_audio; then audio=(-map 0:a:0 -c:a aac -b:a 48k); else audio=(-an); echo "no audio from camera - recording video only" >&2; fi

# fMP4 segments stay playable even if ffmpeg is killed mid-file. hvc1 tag = Safari-compatible HEVC.
ffmpeg -nostdin -hide_banner -loglevel warning \
  -rtsp_transport tcp -timeout 15000000 \
  -i "$URL" \
  -map 0:v:0 -c:v copy -tag:v hvc1 "${audio[@]}" \
  -f segment -segment_time "$SEGMENT_SECONDS" -segment_atclocktime 1 -reset_timestamps 1 \
  -strftime 1 -strftime_mkdir 1 \
  -segment_format mp4 -segment_format_options movflags=+frag_keyframe+empty_moov+default_base_moof \
  "$NVR_DIR/%Y/%m/%d/%H-%M-%S.mp4" &
ff=$!

restart() {
  echo "watchdog: $1 - restarting ffmpeg" >&2
  kill "$ff" 2>/dev/null || true
  sleep 10
  kill -9 "$ff" 2>/dev/null || true
  exit 0
}

# Watchdog. Failure modes seen in practice:
#  - ffmpeg writes nothing while it waits for audio that stopped after has_audio() said yes (left alone,
#    it once produced one 640 MB segment covering 90 minutes, named after the time audio came back);
#  - while recording video only, audio comes back: restart to pick it up (checked every 10 min);
#  - ffmpeg loses the HEVC parameter sets and keeps writing segments with an empty hvcC box, which
#    nothing can decode, until it is restarted (a fresh start re-reads them from the stream).
watchdog() {
  local started checked="" now files last prev seen="" grew probed
  started=$(date +%s)
  grew=$started probed=$started
  while sleep 30; do
    kill -0 "$ff" 2>/dev/null || exit 0
    now=$(date +%s)
    if [ "${audio[0]}" = -an ] && [ $((now - probed)) -ge 600 ]; then
      probed=$now
      ! has_audio || restart "camera audio is back"
    fi
    # this run's segments, oldest first (names are YYYY/MM/DD/HH-MM-SS). On the CIFS share a file's
    # mtime stays at its creation time until it is closed, so progress is judged by size, not mtime.
    files=$(find "$NVR_DIR/$(date -d '-1 day' +%Y/%m/%d)" "$NVR_DIR/$(date +%Y/%m/%d)" -maxdepth 1 \
              -name '*.mp4' -newermt "@$started" 2>/dev/null | sort || true)
    last=$(tail -n1 <<<"$files")
    if [ -n "$last" ]; then
      last="$last $(stat -c %s "$last" 2>/dev/null || echo 0)"
      [ "$last" = "$seen" ] || { seen=$last; grew=$now; }
    fi
    [ $((now - grew)) -lt "$STALL_SECONDS" ] || restart "nothing written for $((now - grew))s"
    [ -n "$last" ] || continue
    # the second-newest segment is finished: make sure it can actually be decoded (checked once)
    [ "$(wc -l <<<"$files")" -ge 2 ] || continue
    prev=$(tail -n2 <<<"$files" | head -n1)
    [ "$prev" != "$checked" ] || continue
    checked=$prev
    ffprobe -v quiet -select_streams v:0 -show_entries stream=width -of csv=p=0 "$prev" | grep -q '[0-9]' \
      || restart "finished segment $prev is not decodable"
  done
}
watchdog &
wd=$!
trap 'kill "$mk" "$wd" "$ff" 2>/dev/null || true' EXIT
set +e
wait "$ff"
rc=$?
set -e
exit "$rc"
