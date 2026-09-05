# NixOS module: two-camera stacked/mixed WebRTC viewer with recording.
#
# Install (survives reboots):
#   1) copy this dir to a stable path, e.g. /etc/nixos/cams/ (with app.py alongside)
#   2) in /etc/nixos/configuration.nix add:   imports = [ ./cams/cams.nix ];
#   3) sudo nixos-rebuild switch
#
# Services: cams-mediamtx (per-camera WebRTC) and cams-app (web page +
# recorder). Recordings persist in /var/lib/cams/recordings.
{ config, lib, pkgs, ... }:

let
  ffmpeg = pkgs.ffmpeg;            # has libx264 + libopus
  mediamtx = pkgs.mediamtx;
  python = pkgs.python3;
  rclone = pkgs.rclone;           # for Google Drive upload

  # Google Drive: remote:path that rclone copies clips into. The rclone remote
  # (auth) lives in /etc/cams/rclone.conf (root 600); upload stays off until it
  # exists. Set this to your remote name + folder.
  gdriveRemote = "gdrive:CameraClips";

  acmeEmail = "roman.nefyodov@gmail.com";   # Let's Encrypt account / expiry notices

  # ---- edit these to match your cameras / network ----
  cam1 = "192.168.1.146";
  cam2 = "192.168.1.185";
  camUser = "admin";
  camPass = "";                   # blank today; set a real password on the cameras
  stream = "sub";                 # 'sub' (light) or 'main' (HD)
  lanHost = "192.168.1.250";      # this box's LAN IP, advertised to WebRTC clients
  webHost = "cam.axonpipe.com";   # the name you browse to (port 80, no custom port)
  webPort = 8088;                 # app backend (fronted by nginx on :80)
  webrtcPort = 8889;
  # ----------------------------------------------------

  rtspPath = "h264Preview_01_${stream}";
  cred = if camUser == "" then "" else "${camUser}:${camPass}@";

  # Resilient per-camera publisher: stream the camera when it's up, publish a
  # black frame + silence when it's down, so downstream (composite) never breaks
  # and a missing camera shows as a blank panel. Args: <camera-ip> <mediamtx-path>
  camPublish = pkgs.writeShellScript "cam-publish" ''
    set -u
    ip="$1"; path="$2"
    cam="rtsp://${cred}$ip:554/${rtspPath}"
    out="rtsp://localhost:8554/$path"
    FF="${ffmpeg}/bin/ffmpeg"
    reachable() { ${pkgs.coreutils}/bin/timeout 3 ${pkgs.bash}/bin/bash -c "exec 3<>/dev/tcp/$ip/554" 2>/dev/null; }
    while true; do
      if reachable; then
        # camera up: stream it (blocks until it drops)
        "$FF" -hide_banner -loglevel warning -nostdin -rtsp_transport tcp \
          -i "$cam" -map 0 -c:v copy -c:a libopus -b:a 64k -ac 2 \
          -f rtsp -rtsp_transport tcp "$out" || true
      else
        # camera down: publish ~10s of black + silence, then re-check
        "$FF" -hide_banner -loglevel warning -nostdin -re \
          -f lavfi -i "color=c=black:s=896x512:r=10" -f lavfi -i "anullsrc=r=48000:cl=stereo" \
          -t 10 -c:v libx264 -preset ultrafast -tune stillimage -pix_fmt yuv420p -g 20 \
          -c:a libopus -b:a 32k -shortest -f rtsp -rtsp_transport tcp "$out" || true
      fi
      ${pkgs.coreutils}/bin/sleep 1
    done
  '';

  # Time-aligned stacked composite of the two resilient streams (blank pane if a
  # cam is down). Each input is stamped with its arrival wall clock and ffmpeg's
  # per-input rebasing is disabled (-copyts), so vstack pairs the frames that
  # arrived at the same moment rather than the Nth frame of each RTSP session
  # (those started ~3.5 s apart). -output_ts_offset shifts the merged output back
  # by the launch epoch so timestamps leave the muxer near zero. A script, not an
  # inline runOnInit, because MediaMTX does not run commands through a shell and
  # the epoch has to be computed at start.
  camComposite = pkgs.writeShellScript "cam-composite" ''
    port="''${RTSP_PORT:-8554}"
    exec ${ffmpeg}/bin/ffmpeg -loglevel warning -nostdin -copyts \
      -use_wallclock_as_timestamps 1 -rtsp_transport tcp -i "rtsp://localhost:$port/cam1" \
      -use_wallclock_as_timestamps 1 -rtsp_transport tcp -i "rtsp://localhost:$port/cam2" \
      -filter_complex "[0:v]scale=640:-2,setsar=1[v0];[1:v]scale=640:-2,setsar=1[v1];[v0][v1]vstack=inputs=2[v];[0:a][1:a]amix=inputs=2:normalize=0,aresample=async=1[a]" \
      -map "[v]" -map "[a]" \
      -c:v libx264 -preset veryfast -tune zerolatency -profile:v baseline -pix_fmt yuv420p -g 20 \
      -c:a libopus -b:a 64k -ac 2 \
      -output_ts_offset "-$(${pkgs.coreutils}/bin/date +%s)" \
      -f rtsp -rtsp_transport tcp "rtsp://localhost:$port/composite"
  '';

  # MediaMTX config generated here so ${ffmpeg} is a real store path (GC-safe).
  mediamtxCfg = pkgs.writeText "mediamtx.yml" ''
    logLevel: info
    readTimeout: 15s
    writeTimeout: 15s
    rtsp: yes
    rtspAddress: :8554
    rtspTransports: [tcp]
    webrtc: yes
    webrtcAddress: :${toString webrtcPort}
    webrtcAdditionalHosts: [${lanHost}]
    webrtcLocalUDPAddress: :8189
    webrtcLocalTCPAddress: :8189
    hls: no
    rtmp: no
    srt: no
    # rolling per-camera buffer for motion clips, served via the playback API
    playback: yes
    playbackAddress: :9996
    recordPath: /var/lib/cams-mediamtx/rec/%path/%Y-%m-%d_%H-%M-%S-%f
    recordFormat: fmp4
    recordSegmentDuration: 60s
    recordDeleteAfter: 10m
    authMethod: internal
    authInternalUsers:
      - user: any
        pass:
        permissions:
          - action: publish
          - action: read
          - action: playback
    paths:
      cam1:
        record: yes
        runOnInit: ${camPublish} ${cam1} cam1
        runOnInitRestart: yes
      cam2:
        record: yes
        runOnInit: ${camPublish} ${cam2} cam2
        runOnInitRestart: yes
      # time-aligned stacked composite (see camComposite above)
      composite:
        runOnInit: ${camComposite}
        runOnInitRestart: yes
  '';

  appPy = ./app.py;   # copied into the store on rebuild
in {
  systemd.services.cams-mediamtx = {
    description = "Cameras: MediaMTX WebRTC server + compositor";
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      ExecStart = "${mediamtx}/bin/mediamtx ${mediamtxCfg}";
      Restart = "always";
      RestartSec = 3;
      DynamicUser = true;
      RuntimeDirectory = "cams-mediamtx";       # writable cwd for the MoQ cert
      WorkingDirectory = "/run/cams-mediamtx";
      StateDirectory = "cams-mediamtx";         # -> /var/lib/cams-mediamtx (rec buffer)
    };
  };

  systemd.services.cams-app = {
    description = "Cameras: web page + recorder";
    wantedBy = [ "multi-user.target" ];
    after = [ "cams-mediamtx.service" "network-online.target" ];
    wants = [ "network-online.target" ];
    path = [ ffmpeg rclone ];
    environment = {
      PORT = toString webPort;
      WEBRTC_PORT = toString webrtcPort;
      FFMPEG = "${ffmpeg}/bin/ffmpeg";
      CAMS_NO_AUTOMTX = "1";                  # systemd runs MediaMTX
      REC_DIR = "/var/lib/cams/recordings";
      PYTHONUNBUFFERED = "1";                 # logs reach journald immediately
      # cameras for motion detection / per-camera snapshots
      CAM_IPS = "${cam1}=Camera 1,${cam2}=Camera 2";
      CAM_USER = camUser;
      CAM_PASS = camPass;
      # --- Google Drive (rclone). Upload activates once rclone.conf exists. ---
      RCLONE = "${rclone}/bin/rclone";
      RCLONE_CONFIG = "/run/cams-app/rclone.conf";   # copied in from /etc/cams below
      GDRIVE_REMOTE = gdriveRemote;
      WHEP_URL = "1";                         # tells the page to use same-origin signaling
    };
    serviceConfig = {
      ExecStart = "${python}/bin/python3 ${appPy}";
      # As root (+), copy the root-only rclone.conf into the service's private
      # runtime dir so the dynamic user can read it. Optional: no-op if absent.
      ExecStartPre = "+${pkgs.bash}/bin/sh -c 'test -f /etc/cams/rclone.conf && install -m0644 /etc/cams/rclone.conf /run/cams-app/rclone.conf || true'";
      Restart = "always";
      RestartSec = 3;
      DynamicUser = true;
      StateDirectory = "cams";                # -> /var/lib/cams (recordings)
      RuntimeDirectory = "cams-app";          # -> /run/cams-app (private)
      RuntimeDirectoryMode = "0700";
      # Optional secrets (Telegram). Leading "-" = don't fail if the file is absent.
      # Put TELEGRAM_BOT_TOKEN=... and TELEGRAM_CHAT_ID=... in this root-only file.
      EnvironmentFile = "-/etc/cams/telegram.env";
    };
  };

  # Reverse proxy on :80 so you browse http://${webHost} with no custom port.
  # "/"           -> the app (page, /status, /record/*)
  # "/cam1/","/cam2/" -> MediaMTX WebRTC signaling (WHEP)
  # WebRTC media still flows directly on 8189 (over the VPN) — not proxied.
  services.nginx = {
    enable = true;
    recommendedProxySettings = true;
    virtualHosts."${webHost}" = {
      default = true;                        # also answer bare-IP requests
      useACMEHost = webHost;                 # serve the DNS-01 cert below
      forceSSL = true;                       # redirect http:80 -> https:443
      locations."/" = {
        proxyPass = "http://127.0.0.1:${toString webPort}";
        proxyWebsockets = true;
      };
      locations."/composite/" = {
        proxyPass = "http://127.0.0.1:${toString webrtcPort}";
        proxyWebsockets = true;
      };
      locations."/cam1/" = {
        proxyPass = "http://127.0.0.1:${toString webrtcPort}";
        proxyWebsockets = true;
      };
      locations."/cam2/" = {
        proxyPass = "http://127.0.0.1:${toString webrtcPort}";
        proxyWebsockets = true;
      };
    };
  };

  # HTTPS cert via Let's Encrypt DNS-01 (Cloudflare) — works for a private,
  # VPN-only IP with no public exposure. Needs a Cloudflare API token with
  # Zone.DNS:Edit on the axonpipe.com zone, in the credentials file below:
  #   CF_DNS_API_TOKEN=<token>
  security.acme = {
    acceptTerms = true;
    defaults.email = acmeEmail;
    certs."${webHost}" = {
      dnsProvider = "cloudflare";
      dnsResolver = "1.1.1.1:53";
      environmentFile = "/etc/cams/acme-cloudflare.env";
      group = "nginx";                       # so nginx can read the cert
    };
  };

  # Open the ports (LAN + VPN). These merge with your existing firewall config.
  # 80 (ACME redirect) + 443 (site); 8189 = WebRTC media. 8088/8889 internal.
  networking.firewall.allowedTCPPorts = [ 80 443 8189 ];
  networking.firewall.allowedUDPPorts = [ 8189 ];
}
