#!/bin/sh
# MediaMTX sur Railway : WHIP/signaling DOIT écouter sur $PORT (sinon 502).
set -eu

PORT="${PORT:-8889}"
API_PORT="${MEDIAMTX_API_PORT:-9997}"
RTSP_PORT="${MEDIAMTX_RTSP_PORT:-8554}"
ICE_TCP_PORT="${MEDIAMTX_ICE_TCP_PORT:-8189}"

# Hostname public sans schéma (ex. azlivemtxn.up.railway.app)
PUBLIC_HOST_RAW="${MEDIAMTX_PUBLIC_HOST:-}"
if [ -z "$PUBLIC_HOST_RAW" ]; then
  echo "[mediamtx] ERREUR: MEDIAMTX_PUBLIC_HOST manquant (ex. azlivemtxn.up.railway.app)" >&2
  exit 1
fi
PUBLIC_HOST=$(printf '%s' "$PUBLIC_HOST_RAW" | sed -e 's|^https://||' -e 's|^http://||' -e 's|/$||')

# Auth Django (réseau privé). Si absent → mode ouvert API-only pour démarrer, publish refusé par défaut.
AUTH_URL="${MEDIAMTX_AUTH_URL:-}"
if [ -z "$AUTH_URL" ]; then
  echo "[mediamtx] WARN: MEDIAMTX_AUTH_URL vide — auth HTTP désactivée (dev only)" >&2
  AUTH_BLOCK=$(
    cat <<'EOF'
authInternalUsers:
  - user: any
    pass:
    permissions:
      - action: api
      - action: publish
      - action: read
      - action: playback
EOF
  )
else
  AUTH_BLOCK=$(
    cat <<EOF
authMethod: http
authHTTPAddress: ${AUTH_URL}
authHTTPExclude:
  - action: api
  - action: metrics
  - action: pprof
EOF
  )
fi

CONFIG_PATH="/mediamtx.railway.yml"

if [ -n "${MEDIAMTX_TURN_URL:-}" ]; then
  ICE_BLOCK=$(
    cat <<EOF
webrtcICEServers2:
  - url: stun:stun.l.google.com:19302
  - url: ${MEDIAMTX_TURN_URL}
    username: "${MEDIAMTX_TURN_USERNAME:-}"
    password: "${MEDIAMTX_TURN_PASSWORD:-}"
EOF
  )
else
  ICE_BLOCK=$(
    cat <<'EOF'
webrtcICEServers2:
  - url: stun:stun.l.google.com:19302
EOF
  )
fi

cat >"$CONFIG_PATH" <<EOF
logLevel: info
logDestinations: [stdout]

api: yes
apiAddress: :${API_PORT}

${AUTH_BLOCK}

webrtc: yes
webrtcAddress: :${PORT}
webrtcEncryption: no
webrtcAllowOrigin: "*"
webrtcTrustedProxies: [0.0.0.0/0]
webrtcAdditionalHosts: [${PUBLIC_HOST}]
webrtcLocalTCPAddress: :${ICE_TCP_PORT}
webrtcLocalUDPAddress: :${ICE_TCP_PORT}
${ICE_BLOCK}

rtsp: yes
rtspAddress: :${RTSP_PORT}

rtmp: no
hls: no
srt: no

paths: {}
EOF

echo "======== MediaMTX Railway ========"
echo "WHIP/signaling  :0.0.0.0:${PORT}  (Railway public HTTPS)"
echo "API contrôle    :0.0.0.0:${API_PORT}  (privé railway.internal)"
echo "RTSP            :0.0.0.0:${RTSP_PORT}"
echo "ICE TCP/UDP     :0.0.0.0:${ICE_TCP_PORT}"
echo "PUBLIC_HOST     ${PUBLIC_HOST}"
echo "AUTH            ${AUTH_URL:-disabled}"
echo "Config:"
cat "$CONFIG_PATH"
echo "=================================="

# Binary path selon l'image officielle
if [ -x /mediamtx ]; then
  exec /mediamtx "$CONFIG_PATH"
elif [ -x /usr/local/bin/mediamtx ]; then
  exec /usr/local/bin/mediamtx "$CONFIG_PATH"
else
  echo "[mediamtx] binaire introuvable" >&2
  ls -la / || true
  exit 1
fi
