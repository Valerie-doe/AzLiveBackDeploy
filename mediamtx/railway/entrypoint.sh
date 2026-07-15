#!/bin/sh
# Génère mediamtx.yml depuis les variables d'environnement Railway, puis démarre MediaMTX.
set -eu

PORT="${PORT:-8889}"
API_PORT="${MEDIAMTX_API_PORT:-9997}"
RTSP_PORT="${MEDIAMTX_RTSP_PORT:-8554}"
ICE_TCP_PORT="${MEDIAMTX_ICE_TCP_PORT:-8189}"

# Auth HTTP → backend Django (réseau privé Railway)
# Ex. http://azliveback.railway.internal:8080/api/media/auth/
AUTH_URL="${MEDIAMTX_AUTH_URL:?MEDIAMTX_AUTH_URL est requis (URL Django /api/media/auth/)}"

# Hostname public du service MediaMTX (sans https://), pour les candidats ICE.
# Ex. azlivemtx.up.railway.app
PUBLIC_HOST="${MEDIAMTX_PUBLIC_HOST:?MEDIAMTX_PUBLIC_HOST est requis (hostname public Railway)}"

CONFIG_PATH="/tmp/mediamtx.railway.yml"

ICE_BLOCK=""
if [ -n "${MEDIAMTX_TURN_URL:-}" ]; then
  TURN_USER="${MEDIAMTX_TURN_USERNAME:-}"
  TURN_PASS="${MEDIAMTX_TURN_PASSWORD:-}"
  ICE_BLOCK=$(
    cat <<EOF
webrtcICEServers2:
  - url: stun:stun.l.google.com:19302
  - url: ${MEDIAMTX_TURN_URL}
    username: "${TURN_USER}"
    password: "${TURN_PASS}"
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
# Auto-généré par entrypoint.sh — ne pas éditer à la main.
logLevel: info
logDestinations: [stdout]

api: yes
apiAddress: :${API_PORT}

authMethod: http
authHTTPAddress: ${AUTH_URL}
authHTTPExclude:
  - action: api
  - action: metrics
  - action: pprof

# WHIP / signaling HTTP (Railway reverse-proxy HTTPS → \$PORT)
webrtc: yes
webrtcAddress: :${PORT}
webrtcEncryption: no
webrtcAllowOrigin: "*"
webrtcTrustedProxies: [0.0.0.0/0]
webrtcAdditionalHosts: [${PUBLIC_HOST}]
# ICE TCP (Railway n'expose en général pas l'UDP public) — publier le port ${ICE_TCP_PORT} en TCP.
webrtcLocalTCPAddress: :${ICE_TCP_PORT}
# Désactive le mux UDP dédié (souvent inutilisable sur PaaS) ; STUN/TURN gèrent le reste.
webrtcLocalUDPAddress: ""
${ICE_BLOCK}

rtsp: yes
rtspAddress: :${RTSP_PORT}

rtmp: no
hls: no
srt: no

paths: {}
EOF

echo "[mediamtx] WHIP/signaling :0.0.0.0:${PORT}"
echo "[mediamtx] API            :0.0.0.0:${API_PORT}"
echo "[mediamtx] RTSP           :0.0.0.0:${RTSP_PORT}"
echo "[mediamtx] ICE TCP/UDP    :0.0.0.0:${ICE_TCP_PORT}"
echo "[mediamtx] authHTTP       ${AUTH_URL}"
echo "[mediamtx] public host    ${PUBLIC_HOST}"

exec /mediamtx "$CONFIG_PATH"
