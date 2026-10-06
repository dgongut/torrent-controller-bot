# Named once, for FROM and for the label that says which base this is.
ARG BASE_IMAGE=alpine:3.24.2
FROM ${BASE_IMAGE}
ARG BASE_IMAGE

ARG VERSION=1.7.0

# Standard OCI labels. The version comes from the same VERSION that picks the
# source tag below, so it cannot say one thing and contain another; it is
# what lets docker-controller-bot — and Watchtower, Diun, Portainer… — announce
# an update of this image with numbers instead of dates.
LABEL org.opencontainers.image.title="torrent-controller-bot" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/dgongut/torrent-controller-bot" \
      org.opencontainers.image.url="https://hub.docker.com/r/dgongut/torrent-controller-bot" \
      org.opencontainers.image.licenses="GPL-3.0" \
      org.opencontainers.image.description="Control your torrent client from a single place: your Telegram." \
      org.opencontainers.image.documentation="https://github.com/dgongut/torrent-controller-bot#readme" \
      org.opencontainers.image.authors="dgongut" \
      org.opencontainers.image.base.name="${BASE_IMAGE}"

# Without it Python buffers stdout when there is no tty and docker logs stays empty
ENV TZ=UTC \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies and download source
RUN apk add --no-cache python3 py3-pip tzdata curl unzip && \
    curl -fsSL https://github.com/dgongut/torrent-controller-bot/archive/refs/tags/v${VERSION}.zip -o /tmp/app.zip && \
    unzip -q /tmp/app.zip -d /tmp && \
    mv /tmp/torrent-controller-bot-${VERSION}/torrent-controller-bot.py /app && \
    mv /tmp/torrent-controller-bot-${VERSION}/bot_settings.py /app && \
    mv /tmp/torrent-controller-bot-${VERSION}/config.py /app && \
    mv /tmp/torrent-controller-bot-${VERSION}/logger.py /app && \
    mv /tmp/torrent-controller-bot-${VERSION}/message_queue.py /app && \
    mv /tmp/torrent-controller-bot-${VERSION}/name_parser.py /app && \
    mv /tmp/torrent-controller-bot-${VERSION}/telemetry.py /app && \
    mv /tmp/torrent-controller-bot-${VERSION}/torrent_clients /app && \
    mv /tmp/torrent-controller-bot-${VERSION}/locale /app && \
    mv /tmp/torrent-controller-bot-${VERSION}/requirements.txt /app && \
    rm -rf /tmp/app.zip /tmp/torrent-controller-bot-${VERSION}/ && \
    apk del --no-cache curl unzip && \
    export PIP_BREAK_SYSTEM_PACKAGES=1 && \
    pip3 install --no-cache-dir -Ur /app/requirements.txt

# Health check: unhealthy when Telegram has not answered a poll for 2 minutes
# (the bot touches the file on every poll, see HEARTBEAT_PATH in config.py)
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python3 -c "import os, sys, time; sys.exit(time.time() - os.path.getmtime('/tmp/torrent-controller-bot.heartbeat') > 120)"

ENTRYPOINT ["python3", "torrent-controller-bot.py"]

# When and from which commit, passed at build time:
#   --build-arg BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ) --build-arg VCS_REF=$(git rev-parse HEAD)
# Last on purpose: they change on every build, and anything after them would
# be rebuilt every time instead of coming from the cache.
ARG BUILD_DATE
ARG VCS_REF
LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}"
