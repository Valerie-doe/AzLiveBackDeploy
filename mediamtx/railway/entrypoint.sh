#!/bin/sh
# MediaMTX sur Railway : WHIP/signaling DOIT écouter sur $PORT (sinon 502).
# ICE TCP/UDP doivent utiliser d'AUTRES ports (sinon "address already in use").
set -eu

PORT="${PORT:-8889}"
API_PORT="${MEDIAMTX_API_PORT:-9997}"
RTSP_PORT="${MEDIAMTX_RTSP_PORT:-8554}"

# Ports ICE distincts du $PORT Railway (crucial : sinon bind conflict).
ICE_TCP_PORT="${MEDIAMTX_ICE_TCP_PORT:-8190}"
ICE_UDP_PORT="${MEDIAMTX_ICE_UDP_PORT:-8191}"

# Si quelqu'un a forcé ICE=8189 alors que PORT=8189 aussi → corrige automatiquement.
if [ "$ICE_TCP_PORT" = "$PORT" ]; then
  ICE_TCP_PORT=8190
  echo "[mediamtx] WARN: ICE_TCP_PORT == PORT ($PORT) → forcé à 8190" >&2
fi
if [ "$ICE_UDP_PORT" = "$PORT" ] || [ "$ICE_UDP_PORT" = "$ICE_TCP_PORT" ]; then
  ICE_UDP_PORT=8191
  echo "[mediamtx] WARN: ICE_UDP_PORT en conflit → forcé à 8191" >&2
fi

PUBLIC_HOST_RAW="${MEDIAMTX_PUBLIC_HOST:-}"
if [ -z "$PUBLIC_HOST_RAW" ]; then
  echo "[mediamtx] ERREUR: MEDIAMTX_PUBLIC_HOST manquant (ex. azlivemtxn.up.railway.app)" >&2
  exit 1
fi
# Sans schéma + lowercase (les domaines Railway sont en minuscules)
PUBLIC_HOST=$(printf '%s' "$PUBLIC_HOST_RAW" | sed -e 's|^https://||' -e 's|^http://||' -e 's|/$||' | tr 'A-Z' 'a-z')

# Auth :
# - Par défaut : auth INTERNE ouverte (publish/read/api).
#   Sur Railway, authHTTP vers https://…up.railway.app depuis le container TIMEOUT
#   (hairpin) → WHIP 401. Le token est validé par le proxy Django /api/media/whip/.
# - Optionnel : MEDIAMTX_AUTH_HTTP=true + MEDIAMTX_AUTH_URL=http://backend.railway.internal:PORT/...
AUTH_URL="${MEDIAMTX_AUTH_URL:-}"
AUTH_HTTP="${MEDIAMTX_AUTH_HTTP:-false}"
if [ "$AUTH_HTTP" = "true" ] && [ -n "$AUTH_URL" ]; then
  case "$AUTH_URL" in
    https://*.up.railway.app*|https://*railway.app*)
      echo "[mediamtx] WARN: AUTH_URL publique Railway → risques de timeout hairpin. Préfère http://….railway.internal:PORT/api/media/auth/" >&2
      ;;
  esac
  AUTH_BLOCK=$(
    cat <<EOF
authMethod: http
authHTTPAddress: ${AUTH_URL}
authHTTPExclude:
  - action: api
  - action: metrics
  - action: pprof
  - action: read
EOF
  )
else
  echo "[mediamtx] Auth interne ouverte — sécurité WHIP via proxy Django (recommandé Railway)" >&2
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
webrtcAllowOrigins: ["*"]
webrtcTrustedProxies: [0.0.0.0/0]
webrtcAdditionalHosts: [${PUBLIC_HOST}]
webrtcLocalTCPAddress: :${ICE_TCP_PORT}
webrtcLocalUDPAddress: :${ICE_UDP_PORT}
${ICE_BLOCK}

rtsp: yes
rtspAddress: :${RTSP_PORT}

# MoQ inutile pour AZLive et consomme RAM/ports (crash possible sur petit plan Railway).
moq: no

rtmp: no
hls: no
srt: no

paths: {}
EOF

echo "======== MediaMTX Railway ========"
echo "WHIP/signaling  :0.0.0.0:${PORT}  (Railway public HTTPS)"
echo "API contrôle    :0.0.0.0:${API_PORT}  (privé railway.internal)"
echo "RTSP            :0.0.0.0:${RTSP_PORT}"
echo "ICE TCP         :0.0.0.0:${ICE_TCP_PORT}"
echo "ICE UDP         :0.0.0.0:${ICE_UDP_PORT}"
echo "PUBLIC_HOST     ${PUBLIC_HOST}"
echo "AUTH            ${AUTH_URL:-disabled}"
echo "Config:"
cat "$CONFIG_PATH"
echo "=================================="

if [ -x /mediamtx ]; then
  exec /mediamtx "$CONFIG_PATH"
elif [ -x /usr/local/bin/mediamtx ]; then
  exec /usr/local/bin/mediamtx "$CONFIG_PATH"
else
  echo "[mediamtx] binaire introuvable" >&2
  ls -la / || true
  exit 1
fi
