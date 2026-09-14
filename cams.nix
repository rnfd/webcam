# NixOS module: two cameras, one live WebRTC stream each (own link per camera),
# recorded together as one stacked/mixed video.
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
  font = "${pkgs.dejavu_fonts.minimal}/share/fonts/truetype/DejaVuSans.ttf";  # placeholder captions

  # Shared on/off switches, written by the app and read by the publishers below.
  # A plain directory (not a service StateDirectory) because DynamicUser puts
  # those under /var/lib/private, which the other service cannot traverse.
  stateDir = "/var/lib/cams-state";

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
  # Live views play the camera's MAIN stream (2880x1616), stream-copied, so the
  # picture on the web is the camera's full quality and the server transcodes
  # nothing. The SUB stream (896x512) is published alongside it for the page's
  # SD toggle, for the always-on compositor, and for the motion-clip buffer —
  # all of which are cheaper and no worse at those jobs.
  mainStream = "main";
  subStream = "sub";
  lanHost = "192.168.1.250";      # this box's LAN IP, advertised to WebRTC clients
  webHost = "cam.axonpipe.com";   # the name you browse to (port 80, no custom port)
  webPort = 8088;                 # app backend (fronted by nginx on :80)
  webrtcPort = 8889;
  mtxApiPort = 9997;              # MediaMTX control API, loopback only (/close kicks viewers)

  # ---- /expose and /close: the site without the VPN ----
  # Normally ${webHost} resolves to ${lanHost}, which only the LAN and the VPN
  # can reach. /expose repoints that DNS record at the Cloudflare tunnel below
  # (a proxied CNAME) and /close puts the LAN address back. The tunnel's ingress
  # for ${webHost} lands on a second nginx vhost that listens on loopback only
  # and refuses everything unless the "exposed" switch file exists — so the
  # tunnel route is dead while closed even before DNS has caught up.
  #
  # Page and WHEP signalling go through the tunnel (plain HTTP); the WebRTC media
  # itself cannot — it needs a direct path to this box on 8189. MediaMTX learns
  # its public address through STUN and advertises it, and /expose asks the
  # router to forward 8189 TCP+UDP to ${lanHost} (see routerUrl below).
  tunnelId = "fd428774-b936-4168-9b20-79c18cfca78e";   # services.cloudflared tunnel in configuration.nix
  publicPort = 8090;              # loopback vhost the tunnel ingress points at
  cfZone = "axonpipe.com";
  cfTokenFile = "/etc/cloudflare-ddns.token";  # Zone:DNS:Edit on ${cfZone} (root, 0400), shared with cloudflare-ddns
  # The router (a Linksys Velop, JNAP API): /expose adds a single-port forward
  # of 8189 TCP+UDP to ${lanHost} and /close removes it. Needs the admin
  # password in /etc/cams/router.env (root, 0600) as ROUTER_PASS=...; without
  # the file the commands still work, minus the forward.
  routerUrl = "http://192.168.1.1";
  # ----------------------------------------------------

  cred = if camUser == "" then "" else "${camUser}:${camPass}@";

  # "/"           -> the app: "/" both cameras, "/1" and "/2" one each
  #                  (separate live links), plus /status and /record/*
  # "/cam1/","/cam2/" -> MediaMTX WebRTC signaling (WHEP), one per camera
  # WebRTC media still flows directly on 8189 — not proxied.
  # Shared by the VPN vhost and the tunnel (public) vhost below.
  camLocations = {
    "/" = {
      proxyPass = "http://127.0.0.1:${toString webPort}";
      proxyWebsockets = true;
    };
  } // lib.genAttrs [ "/composite/" "/cam1/" "/cam2/" "/cam1sub/" "/cam2sub/" ] (_: {
    proxyPass = "http://127.0.0.1:${toString webrtcPort}";
    proxyWebsockets = true;
  });

  # Per-camera publisher — see cam-publish.sh for what it does. Kept as a file
  # next to this module so local dev (mediamtx.yml) runs the exact same logic;
  # the store paths it needs arrive through the service environment below.
  camPublish = pkgs.writeShellScript "cam-publish" (builtins.readFile ./cam-publish.sh);

  # Time-aligned stacked composite of the two SUB streams — see cam-composite.sh
  # for what it does and why. Kept as a file next to this module, like
  # cam-publish.sh, so local dev (mediamtx.yml) runs the exact same encode; the
  # store paths it needs arrive through the service environment below. A script,
  # not an inline runOnInit, because MediaMTX does not run commands through a
  # shell and the launch epoch has to be computed at start.
  camComposite = pkgs.writeShellScript "cam-composite" (builtins.readFile ./cam-composite.sh);

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
    # Public viewers (/expose) are behind NAT on both ends: STUN lets the server
    # discover and advertise its public address (the router still has to forward
    # 8189 here). VPN viewers keep using the LAN candidate above.
    webrtcICEServers2:
      - url: stun:stun.l.google.com:19302
    webrtcTrustedProxies: [127.0.0.1]   # nginx: log the viewer's address, not nginx's
    # control API, loopback only: /close uses it to drop public viewers
    api: yes
    apiAddress: 127.0.0.1:${toString mtxApiPort}
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
          - action: api
    paths:
      # HD: what the page plays by default — the camera's main stream, copied.
      # /timelapse reads it live, so it is not recorded.
      cam1:
        runOnInit: ${camPublish} ${cam1} cam1 ${mainStream}
        runOnInitRestart: yes
      cam2:
        runOnInit: ${camPublish} ${cam2} cam2 ${mainStream}
        runOnInitRestart: yes
      # SD: the page's low-bandwidth toggle, the compositor's input, and the
      # rolling buffer motion clips are cut from (record: yes here, not on the
      # HD paths, so the 10-minute buffer stays small).
      cam1sub:
        record: yes
        runOnInit: ${camPublish} ${cam1} cam1sub ${subStream}
        runOnInitRestart: yes
      cam2sub:
        record: yes
        runOnInit: ${camPublish} ${cam2} cam2sub ${subStream}
        runOnInitRestart: yes
      # time-aligned stacked composite (see camComposite above)
      composite:
        runOnInit: ${camComposite}
        runOnInitRestart: yes
  '';

  appPy = ./app.py;   # copied into the store on rebuild
in {
  # Both services join this group so they can share ${stateDir}: the app writes
  # the on/off switches, the publishers read them.
  users.groups.cams = {};
  systemd.tmpfiles.rules = [ "d ${stateDir} 0770 root cams -" ];

  systemd.services.cams-mediamtx = {
    description = "Cameras: MediaMTX WebRTC server + compositor";
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    path = [ pkgs.coreutils pkgs.bash ffmpeg ];   # the publish/composite scripts: timeout, sleep, date
    environment = {
      FF = "${ffmpeg}/bin/ffmpeg";
      FONT = font;                                # captions on the placeholders
      CAMS_STATE = stateDir;                      # the /disable switch
      CAM_USER = camUser;
      CAM_PASS = camPass;
    };
    serviceConfig = {
      ExecStart = "${mediamtx}/bin/mediamtx ${mediamtxCfg}";
      Restart = "always";
      RestartSec = 3;
      DynamicUser = true;
      SupplementaryGroups = [ "cams" ];         # read the on/off switches
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
      CAMS_STATE = stateDir;                  # /enable /disable /follow live here
      PYTHONUNBUFFERED = "1";                 # logs reach journald immediately
      # cameras for motion detection / per-camera snapshots
      CAM_IPS = "${cam1}=Camera 1,${cam2}=Camera 2";
      CLIP_PATH = "cam{n}sub";                # motion clips cut from the sub buffer
      CAM_USER = camUser;
      CAM_PASS = camPass;
      # --- Google Drive (rclone). Upload activates once rclone.conf exists. ---
      RCLONE = "${rclone}/bin/rclone";
      RCLONE_CONFIG = "/run/cams-app/rclone.conf";   # copied in from /etc/cams below
      GDRIVE_REMOTE = gdriveRemote;
      WHEP_URL = "1";                         # tells the page to use same-origin signaling
      # --- /expose and /close (see the let block) ---
      WEB_HOST = webHost;
      LAN_IP = lanHost;
      CF_ZONE = cfZone;
      CF_TUNNEL = tunnelId;
      MTX_API = "http://127.0.0.1:${toString mtxApiPort}";
      ROUTER_URL = routerUrl;
      WEBRTC_MEDIA_PORT = "8189";
    };
    serviceConfig = {
      ExecStart = "${python}/bin/python3 ${appPy}";
      # As root (+), copy the root-only rclone.conf into the service's private
      # runtime dir so the dynamic user can read it. Optional: no-op if absent.
      ExecStartPre = "+${pkgs.bash}/bin/sh -c 'test -f /etc/cams/rclone.conf && install -m0644 /etc/cams/rclone.conf /run/cams-app/rclone.conf || true'";
      Restart = "always";
      RestartSec = 3;
      DynamicUser = true;
      SupplementaryGroups = [ "cams" ];       # write the on/off switches
      # DynamicUser implies ProtectSystem=strict, which mounts /var/lib read-only
      # apart from this service's own StateDirectory — the shared switch directory
      # has to be punched through explicitly or /disable fails with EROFS.
      ReadWritePaths = [ stateDir ];
      StateDirectory = "cams";                # -> /var/lib/cams (recordings)
      RuntimeDirectory = "cams-app";          # -> /run/cams-app (private)
      RuntimeDirectoryMode = "0700";
      # Optional secrets (Telegram). Leading "-" = don't fail if the file is absent.
      # Put TELEGRAM_BOT_TOKEN=... and TELEGRAM_CHAT_ID=... in this root-only file.
      # ROUTER_PASS=... in router.env lets /expose forward the media port itself.
      EnvironmentFile = [ "-/etc/cams/telegram.env" "-/etc/cams/router.env" ];
      # Cloudflare DNS token for /expose and /close, handed to the dynamic user
      # the same way cloudflare-ddns gets it (the app reads
      # $CREDENTIALS_DIRECTORY/cf-token). The file must exist or the unit fails.
      LoadCredential = [ "cf-token:${cfTokenFile}" ];
    };
  };

  # Reverse proxy on :80 so you browse http://${webHost} with no custom port
  # (see camLocations above for what goes where).
  services.nginx = {
    enable = true;
    recommendedProxySettings = true;
    virtualHosts."${webHost}" = {
      default = true;                        # also answer bare-IP requests
      useACMEHost = webHost;                 # serve the DNS-01 cert below
      forceSSL = true;                       # redirect http:80 -> https:443
      locations = camLocations;
    };
    # The same site for the Cloudflare tunnel (/expose). Loopback only, plain
    # HTTP (Cloudflare terminates TLS at its edge), and shut unless the
    # "exposed" switch file exists. Public viewers only watch: the record and
    # pan/tilt endpoints are refused here, and the header tells the app to
    # serve the page without those controls.
    virtualHosts."${webHost}-public" = {
      serverName = webHost;
      listen = [ { addr = "127.0.0.1"; port = publicPort; } ];
      extraConfig = ''
        if (!-f ${stateDir}/exposed) { return 403; }
      '';
      locations = camLocations // {
        "/" = camLocations."/" // { extraConfig = "proxy_set_header X-Cams-Public 1;"; };
        "~ ^/(record|ptz)" = { return = "403"; };
      };
    };
  };
  # nginx has to see into the switch directory (0770 root:cams) for the test above.
  users.users.nginx.extraGroups = [ "cams" ];

  # Tunnel ingress for the site: only reachable while DNS points here (/expose),
  # and only answered while the switch file exists (vhost above).
  services.cloudflared.tunnels."${tunnelId}".ingress."${webHost}" = "http://127.0.0.1:${toString publicPort}";

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
  # 80 (ACME redirect) + 443 (site); 8189 = WebRTC media (/expose forwards it
  # on the router as well). 8088/8889/9997 internal.
  networking.firewall.allowedTCPPorts = [ 80 443 8189 ];
  networking.firewall.allowedUDPPorts = [ 8189 ];
}
