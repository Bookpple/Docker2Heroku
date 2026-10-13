ARG AXONHUB_IMAGE
ARG RUNTIME_IMAGE=debian:bookworm-slim
FROM ${AXONHUB_IMAGE} AS upstream

FROM ${RUNTIME_IMAGE}
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y \
       --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=upstream /app/axonhub /app/axonhub
COPY heroku-entrypoint.sh /app/heroku-entrypoint.sh
RUN chmod 755 /app/axonhub /app/heroku-entrypoint.sh

ENTRYPOINT ["/app/heroku-entrypoint.sh"]
CMD []
