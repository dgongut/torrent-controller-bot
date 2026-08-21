FROM alpine:3.23.5

ARG VERSION=1.2.2

ENV TZ=UTC

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
    mv /tmp/torrent-controller-bot-${VERSION}/torrent_clients /app && \
    mv /tmp/torrent-controller-bot-${VERSION}/locale /app && \
    mv /tmp/torrent-controller-bot-${VERSION}/requirements.txt /app && \
    rm -rf /tmp/app.zip /tmp/torrent-controller-bot-${VERSION}/ && \
    apk del --no-cache curl unzip && \
    export PIP_BREAK_SYSTEM_PACKAGES=1 && \
    pip3 install --no-cache-dir -Ur /app/requirements.txt

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python3 -c "import sys; sys.exit(0)" || exit 1

ENTRYPOINT ["python3", "torrent-controller-bot.py"]
