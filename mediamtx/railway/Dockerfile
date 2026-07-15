# Image officielle MediaMTX + ffmpeg, adaptée au $PORT Railway.
FROM bluenviron/mediamtx:1-ffmpeg

USER root

# Alpine/busybox shell présent sur l'image -ffmpeg
COPY entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

# WHIP ($PORT), API (9997), RTSP (8554), ICE (8189)
EXPOSE 8889 9997 8554 8189

# Écrase l'entrypoint par défaut de l'image (/mediamtx)
ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
