#!/usr/bin/env bash
# Stack the two SUB streams into the `composite` path, time-aligned.
#
# Nothing on the web plays this; it exists so a recording — and a Telegram
# snapshot — is one combined video. Built from sub, not main, because a
# 2880x1616 decode+encode pair would burn cores to produce a downscaled stack
# anyway, and it keeps each camera down to one RTSP session per stream.
#
# Time alignment: each input is stamped with its arrival wall clock and ffmpeg's
# per-input rebasing is disabled (-copyts), so vstack pairs the frames that
# arrived at the same moment rather than the Nth frame of each RTSP session
# (those start seconds apart). -output_ts_offset shifts the merged output back by
# the launch epoch so timestamps leave the muxer near zero.
#
# An input that ends must end the encode (shortest=1 / duration=shortest).
# Without that, vstack's default is to keep repeating the last frame of a
# secondary input after it stops, for as long as the first input lasts — so when
# cam2sub blinked (the camera's publisher reconnecting, or its OFFLINE
# placeholder giving way to the real stream) the stack carried on with a live
# camera 1 over a frozen camera 2, indefinitely, and nothing logged it. Now the
# encoder exits, and the loop below brings it back the moment both paths are
# publishing again: a camera blip costs the composite a few seconds, not the
# rest of the session. The inputs also carry a socket timeout, so an upstream
# publisher that stalls without disconnecting ends this encode too instead of
# leaving it blocked in a read.
#
# The loop is here rather than left to MediaMTX (runOnInitRestart) because
# MediaMTX waits 5 s before relaunching a runOnInit command; a 1 s retry keeps
# the gap short. ffmpeg fails at once when a path has no publisher, so the retry
# is cheap.
#
# While the cameras are off nothing publishes cam1sub/cam2sub, so there is
# nothing to stack: this waits for the switch instead of reconnect-looping
# against two dead paths, and stops the encoder the moment the switch flips
# mid-run — an idle compositor is one sleeping shell, not a 5%-of-a-core encode.
#
# Env: FF (ffmpeg), CAMS_STATE, RTSP_PORT, STALL_TIMEOUT (seconds).
set -u

FF="${FF:-ffmpeg}"
STATE="${CAMS_STATE:-./state}"
off="$STATE/disabled"
port="${RTSP_PORT:-8554}"
stall_us=$(( ${STALL_TIMEOUT:-10} * 1000000 ))   # ffmpeg wants microseconds

while true; do
  while [ -e "$off" ]; do sleep 2; done

  "$FF" -loglevel warning -nostdin -copyts \
    -use_wallclock_as_timestamps 1 -rtsp_transport tcp -timeout "$stall_us" -i "rtsp://localhost:$port/cam1sub" \
    -use_wallclock_as_timestamps 1 -rtsp_transport tcp -timeout "$stall_us" -i "rtsp://localhost:$port/cam2sub" \
    -filter_complex "[0:v]scale=640:-2,setsar=1[v0];[1:v]scale=640:-2,setsar=1[v1];[v0][v1]vstack=inputs=2:shortest=1[v];[0:a][1:a]amix=inputs=2:duration=shortest:normalize=0,aresample=async=1[a]" \
    -map "[v]" -map "[a]" \
    -c:v libx264 -preset veryfast -tune zerolatency -profile:v baseline -pix_fmt yuv420p -g 20 \
    -c:a libopus -b:a 64k -ac 2 \
    -output_ts_offset "-$(date +%s)" \
    -f rtsp -rtsp_transport tcp "rtsp://localhost:$port/composite" &
  pid=$!

  # Stop the encoder as soon as the cameras go off; otherwise wait for it to end
  # (an input went away) and go round again.
  while kill -0 "$pid" 2>/dev/null; do
    [ -e "$off" ] && break
    sleep 1
  done
  # TERM first; a process still alive 3 s later gets KILL (same as cam-publish).
  kill "$pid" 2>/dev/null || true
  for _ in 1 2 3; do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  kill -9 "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  sleep 1
done
