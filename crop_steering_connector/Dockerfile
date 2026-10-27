# Supervisor 2026.04.0 and later no longer pass BUILD_FROM, so the base image has a default.
ARG BUILD_FROM=ghcr.io/home-assistant/base:3.24
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 ca-certificates

COPY connector /app/connector
COPY start.py /app/start.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh \
    && python3 -m compileall -q /app

CMD [ "/run.sh" ]
