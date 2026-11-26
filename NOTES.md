# Camera findings

Notes from reverse-engineering a cheap "iCam365 / CloudCam" Wi-Fi PTZ camera (sold under brands such as
BOYKEEP, model Y108-1PC; ONVIF reports manufacturer EYEPLUS, firmware 57.1.0.0). Other cameras built on the
same platform very likely behave the same. IPs below use the placeholder `192.168.1.50`.

## RTSP works, no auth
- Main stream `rtsp://192.168.1.50:554/0/av0`, sub stream `rtsp://192.168.1.50:554/0/av1`. Both H.265 + PCMA audio.
- Server banner: "TAS-Tech Streaming Server V100R001". Answers 200 for almost any path, but only ever serves the live stream.
- No credentials required, so anyone on the LAN can watch the camera -> isolate it (see "Security" below).
- Joining mid-GOP gives a few HEVC "Could not find ref" errors; harmless.

## Open ports
- 80 (HTTP + ONVIF), 554 (RTSP), 6670 (raw service, probably the cloud/P2P app channel), 8001 (second RTSP server).

## ONVIF / PTZ - works
- Device service: `POST http://192.168.1.50/onvif/device_service` (SOAP; a plain GET returns an empty reply).
- Services: Device, Events, Imaging, Media, PTZ, DeviceIO under `http://192.168.1.50/onvif/<Name>`.
- Profiles: Profile_1 = main stream, Profile_2 = sub stream. PTZ token: `PTZToken`.
- ContinuousMove/Stop are accepted with no auth. The camera drops the connection on GetPresets, so
  `/api/ptz/presets` returns `[]`.
- Home Assistant: ONVIF integration, host = camera IP, port 80, blank username/password (or `admin` / anything).

## SD-card playback - not available locally
- No ONVIF Recording/Search/Replay service; RTSP `Range:` requests are ignored (live only).
- SD recordings are only reachable through the vendor app / cloud-P2P protocol. The NVR makes the SD card a redundant backup.

## Recorder lessons
- The camera sometimes stops sending audio for long stretches while the SDP still lists PCMA. With `-c:a aac`
  ffmpeg then writes nothing until audio returns. `recorder.sh` probes for audio before starting (video-only if none,
  re-probes every 10 min) and has a watchdog that restarts ffmpeg if nothing is written for 90 s or a finished
  segment is not decodable.
- Some runs produced segments with an empty `hvcC` box (unplayable). The watchdog now catches these.
- ffmpeg 7.1 does not create nested strftime directories, so `recorder.sh` pre-creates today's and tomorrow's.
- Measured bitrate is about 50 KB/s (~4 GB/day, ~130 GB for 30 days) for a 1080p H.265 stream.
- The camera's on-screen clock may drift by several seconds from the server clock.

## Security
- Block the camera from the internet (router rule: block src camera IP -> WAN) so it stops talking to the vendor cloud.
- Put it on an isolated VLAN, reachable only from the machine running SentinelNVR.
