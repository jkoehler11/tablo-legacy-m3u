# One image for both services: the bridge needs ffmpeg, the EPG filter does not.
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY tablo_bridge.py epg_gen.py /app/
ENV PORT=8050 \
    GUIDE_XML=/data/guide.xml \
    EPG_CONFIG=/data/channels.json \
    EPG_OUT=/data/guide.xml
EXPOSE 8050
CMD ["python3", "-u", "/app/tablo_bridge.py"]
