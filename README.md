# webcam

Two Reolink cameras, each its own low-latency WebRTC stream, shown side by side
as two independent panes on one page — with server-side recording, an 8-hour cap,
and delivery of each clip to Telegram (as a link) and Google Drive. Runs as
NixOS services.

## Components

| File | Purpose |
|------|---------|
| `app.py` | Web page (one WebRTC pane per camera + record button), recorder, Telegram control, Drive/Telegram delivery |
| `cams.nix` | NixOS module: MediaMTX + app services, nginx reverse proxy, HTTPS (ACME/Cloudflare DNS), firewall |
| `mediamtx.yml` | Standalone MediaMTX config for local dev (`run.sh`) |
| `run.sh` | Launch MediaMTX + the app locally without NixOS |
| `Caddyfile` | Alternative reverse-proxy config (nginx is used in the module) |
| `snap.py` | One-off JPEG snapshot from a camera |

Environment knobs worth knowing: `PTZ_SPEED` (default 32), `PTZ_MAX_MOVE`
(seconds a pan/tilt may run without a release, default 4), `TL_SPEED` (speed-up
when `/timelapse` names none, default 100), `TL_FPS` (film frame rate, default
30), `TL_WIDTH`/`TL_HEIGHT` (film size, default 1920x1078), `TL_CRF` (default 23)
and `TL_MAX_HOURS` (default 24).

## How it works

- **Per-camera streams:** each camera is published twice — `cam1` / `cam2` carry
  its **main** stream (2880x1616 @ 20 fps, what the page plays) and
  `cam1sub` / `cam2sub` its small one (896x512). Video is stream-copied in both
  cases, so the web picture is the camera's own quality and the server re-encodes
  nothing; only the audio is transcoded, because WebRTC needs Opus. A camera that
  is unreachable publishes black + silence instead, so its pane goes blank rather
  than dying.
- **Viewing — one link per camera:** `https://<host>/1` and `https://<host>/2`
  are separate live pages, one camera each, so you can keep two windows open
  side by side; `https://<host>/` shows both in one page. Either way each camera
  is its own WHEP player — separate peer connection, separate reconnect loop —
  so one camera dropping leaves the other alone. Panes stack on a phone and sit
  side by side on a wide screen; each has its own 🔊 button (only one plays audio
  at a time) and an **HD/SD** button that swaps that pane to the small stream when
  the link is too thin for the main one. nginx fronts it all so there is no custom
  port.
- **Compositing (for storage only):** a second ffmpeg stacks the two *sub*
  streams (`vstack`) and mixes the audio (`amix`) into a `composite` path — sub,
  because it runs 24/7 and the stack is downscaled anyway. Nothing on the
  web plays it — it exists so that what lands on disk and in Google Drive is a
  single combined video with both cameras in it, rather than two files.
  The panes are time-aligned: each input is stamped with its arrival wall clock
  (`-use_wallclock_as_timestamps 1`, `-copyts`), so the stack pairs frames that
  arrived at the same moment instead of the Nth frame of each RTSP session,
  which could be seconds apart.
- **Moving the cameras:** both are Reolink E1 Pro, which pan and tilt, so each
  pane carries a ✥ button and a small arrow pad — hold an arrow to move, release
  to stop; on a single-camera page the keyboard arrows do the same. The page only
  draws the pad for cameras that report `ptzCtrl` + `ptzDirection`. Because a
  Reolink motor runs until it is told to stop, every move also arms a server-side
  stop (`PTZ_MAX_MOVE`, 4 s), so a closed tab or a dropped connection cannot leave
  a camera spinning.
- **Recording:** the app stream-copies that composite to an `.mkv` (no re-encode,
  no extra camera load), so live viewing stays per-camera while storage stays
  combined. Stop finalizes the file, then in the background it
  remuxes to MP4, uploads to Google Drive (rclone), and posts a link to Telegram
  with an upload progress bar. Recordings auto-stop after 8 hours.
- **Timelapse:** `/timelapse 8h 100x` films both cameras for 8 hours and plays
  it back 100 times faster — 4m 48s of film per camera. One ffmpeg per camera
  reads the HD stream live and keeps a frame every speed ÷ fps seconds (3.3 s
  here), encoding it straight into the film, so nothing piles up on disk. It
  decodes every frame rather than just keyframes (those come every 2 s, which
  would make the film stutter); that costs about a fifth of a core per camera.
  A camera that drops, or `/disable`, ends that camera's current piece and a new
  one starts when it is back; the pieces share one encoding and size, so they
  join with a stream copy. At the end each camera's film goes to Google Drive
  with the link posted to Telegram. `/timelapse stop` finishes early and still
  sends what it has; `/timelapse` on its own reports progress.
- **Control:** the web button records; the Telegram bot carries the rest.
  `/enable` and `/disable` are the master switch. `/disable` turns the cameras
  themselves off as far as the network allows — there is no power or sleep command
  on an E1 Pro, so it does what Reolink's own privacy mode does: saves where each
  camera is looking as a PTZ preset, tilts the lens down into the base, and
  switches off the camera's own recording, push alerts, IR and status LEDs. On top
  of that the publishers stop opening RTSP sessions entirely, so nothing is
  captured — and nothing is published at all, so MediaMTX stops recording those
  paths, the compositor stops encoding, and an idle camera costs one sleeping
  shell rather than an ffmpeg. The page reads the same switch: each pane draws a
  "cameras off" panel, the bar shows a ⏸ badge and the record button greys out.
  `/enable` puts back exactly the settings that were there (saved in
  `camera-settings.json`) and recalls the saved view —
  twice, because the head settles a few degrees off on the first recall after
  driving into the tilt stop. `/follow` and `/unfollow` turn detection alerts on and
  off (independent of recording — following works whether or not you are
  recording). `/record` and `/stop` mirror the web button, `/status` says
  what is on right now, and `/clear` deletes the bot's messages from the chat —
  every one it sent in the last 48 hours, which is as far back as Telegram lets a
  bot delete (it notes what it sends in `tg-sent.json`, since a bot cannot read
  chat history). Both switches are files in `/var/lib/cams-state`, shared
  with the MediaMTX service through the `cams` group, so they survive a restart.

## Configuration

Edit the `let` block in `cams.nix` (camera IPs, credentials, streams, host).
State (the `/disable` and `/follow` switches) lives in `/var/lib/cams-state`.
Secrets live outside the repo, in root-only files:

- `/etc/cams/telegram.env` — `TELEGRAM_BOT_TOKEN=` and `TELEGRAM_CHAT_ID=`
- `/etc/cams/rclone.conf` — rclone Google Drive remote named `gdrive`
- `/etc/cams/acme-cloudflare.env` — `CF_DNS_API_TOKEN=` for the HTTPS cert

## Install (NixOS)

Copy `cams.nix` and `app.py` to `/etc/nixos/cams/`, add
`./cams/cams.nix` to your `imports`, then `nixos-rebuild switch`.

## Local dev (no NixOS)

`./run.sh` starts MediaMTX (`mediamtx.yml`) and the app on port 8088.
