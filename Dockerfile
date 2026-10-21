# Official pinned multiarchitecture index: linux/amd64 and linux/arm64, registry-verified.
ARG HERMES_IMAGE=nousresearch/hermes-agent@sha256:444b42594e7f467425872fc35e077a227d377f0fe0726bc818706d62175fad8b
FROM ${HERMES_IMAGE}
USER root
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends xvfb x11-utils tigervnc-scraping-server novnc websockify openbox xterm supervisor chromium fonts-dejavu-core fonts-liberation fonts-noto-color-emoji tzdata && rm -rf /var/lib/apt/lists/*
COPY runtime/ /opt/browser-box/
RUN chmod 755 /opt/browser-box/start.sh /opt/browser-box/launch-hermes.sh /opt/browser-box/discovery-api/hermes_discovery.py && mkdir -p /opt/data && chown 10000:10000 /opt/data
ENV HOME=/opt/data HERMES_HOME=/opt/data DISPLAY=:99
USER 10000:10000
WORKDIR /opt/data/workspace
ENTRYPOINT ["/opt/browser-box/start.sh"]
HEALTHCHECK --interval=10s --start-period=60s --retries=12 CMD python3 /opt/browser-box/health.py
