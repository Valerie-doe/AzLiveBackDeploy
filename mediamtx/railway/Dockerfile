# MediaMTX (+ ffmpeg) pour AZLive sur Railway.
# Image officielle : WHIP → RTSP local → ffmpeg → RTMPS Facebook.
FROM bluenviron/mediamtx:latest-ffmpeg

USER root
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# PORT = WHIP HTTP (Railway injecte $PORT et le proxifie en HTTPS)
# 9997 API contrôle (Django via private networking)
# 8554 RTSP interne (ffmpeg runOnReady)
# 8189 ICE TCP/UDP (à publier en TCP sur Railway si la connexion WebRTC échoue)
EXPOSE 8889 9997 8554 8189

ENTRYPOINT ["/entrypoint.sh"]
