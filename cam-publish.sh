#!/usr/bin/env bash
# Publish one camera to MediaMTX, or a captioned placeholder in its place.
#
#   cam-publish.sh <camera-ip> <mediamtx-path> <main|sub>
#
# Three states, and the switch between them is checked every second — including
# while a stream is running, because an ffmpeg pulling a healthy camera blocks
# forever and would otherwise ignore the switch until the camera dropped:
#
#   $CAMS_STATE/disabled exists -> nothing is published at all: no RTSP session
#                                  to the camera, and no encoder standing in for
#                                  it either (Telegram /disable)
#   camera unreachable          -> "CAMERA OFFLINE", one continuous stream that
#                                  ends only when the camera answers again
#   otherwise                   -> the camera, video stream-copied at its own
#                                  quality; only the audio becomes Opus, which
#                                  is what WebRTC needs
#
# The placeholder used to be cut into 10 s clips so the camera got re-probed
# between them, which left the path unpublished for a second or two every 10 s.
# The compositor downstream now (rightly) ends its encode whenever an input
# ends, so those gaps would have restarted it every 10 s for as long as a camera
# was down. One uninterrupted placeholder, probed from the side, means a camera
# outage costs the composite two short gaps — one going down, one coming back.
#
# The camera input carries a socket timeout (STALL_TIMEOUT, default 10 s). Without
# it an RTSP-over-TCP read blocks forever when a camera reboots or the Wi-Fi
# drops mid-stream: the TCP session stays "established", ffmpeg sits in the read,
# MediaMTX drops the silent publisher after readTimeout, and the path stays dead
# until someone kills the process by hand — it does not even answer SIGTERM in
# that state, which is why the stop below escalates to SIGKILL.
#
# Off means off: an idle publisher costs one sleeping shell, and because the path
# goes unpublished MediaMTX stops recording it too. The page reads the same
# switch and draws its own "cameras off" panel, so there is nothing for a
# placeholder stream to caption. The OFFLINE placeholder stays, because there
# one camera is still live and the compositor downstream needs both its inputs.
#
# Env: FF (ffmpeg), FONT (ttf for the captions; plain black without it),
#      CAMS_STATE, CAM_USER, CAM_PASS, RTSP_PORT, STALL_TIMEOUT (seconds).
set -u
ip="$1"; path="$2"; variant="$3"

FF="${FF:-ffmpeg}"
FONT="${FONT:-}"
STATE="${CAMS_STATE:-./state}"
off="$STATE/disabled"
user="${CAM_USER:-admin}"; pass="${CAM_PASS:-}"
port="${RTSP_PORT:-8554}"
stall_us=$(( ${STALL_TIMEOUT:-10} * 1000000 ))   # ffmpeg wants microseconds
out="rtsp://localhost:$port/$path"
cam="rtsp://$user:$pass@$ip:554/h264Preview_01_$variant"

reachable() { timeout 3 bash -c "exec 3<>/dev/tcp/$ip/554" 2>/dev/null; }
caption() {
  if [ -n "$FONT" ]; then
    echo "drawtext=text=$1:fontfile=$FONT:fontcolor=0x9ca3af:fontsize=40:x=(w-text_w)/2:y=(h-text_h)/2"
  else
    echo "null"
  fi
}

# Run a publisher in the background and stop it the moment the cameras are
# switched off, so /disable takes effect within a second — or the moment the
# given check succeeds (the placeholder ends when the camera is reachable).
never() { false; }
watch_run() {   # <stop-check> <command...>
  local until="$1"; shift
  "$@" &
  local pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    [ -e "$off" ] && break
    "$until" && break
    sleep 1
  done
  # TERM first; a process still alive 3 s later is wedged in a socket read
  # (see above) and only KILL gets it out.
  kill "$pid" 2>/dev/null || true
  for _ in 1 2 3; do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  kill -9 "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

placeholder() {   # <caption>; runs until the camera answers or the switch flips
  watch_run reachable "$FF" -hide_banner -loglevel warning -nostdin -re \
    -f lavfi -i "color=c=black:s=896x512:r=10" -f lavfi -i "anullsrc=r=48000:cl=stereo" \
    -vf "$(caption "$1")" \
    -c:v libx264 -preset ultrafast -tune stillimage -pix_fmt yuv420p -g 20 \
    -c:a libopus -b:a 32k -shortest -f rtsp -rtsp_transport tcp "$out"
}

while true; do
  if [ -e "$off" ]; then
    while [ -e "$off" ]; do sleep 2; done    # idle: one sleeping shell, no encoder
  elif reachable; then
    watch_run never "$FF" -hide_banner -loglevel warning -nostdin -rtsp_transport tcp \
      -timeout "$stall_us" -i "$cam" -map 0 -c:v copy -c:a libopus -b:a 64k -ac 2 \
      -f rtsp -rtsp_transport tcp "$out"
  else
    placeholder "CAMERA OFFLINE"
  fi
  sleep 1
done
