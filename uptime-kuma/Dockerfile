ARG UPTIME_KUMA_IMAGE
FROM ${UPTIME_KUMA_IMAGE}

WORKDIR /app
COPY --chmod=755 heroku-entrypoint.sh exec-command.sh /app/

ENV NODE_ENV=production \
    DATA_DIR=/tmp/uptime-kuma

ENTRYPOINT ["/usr/bin/dumb-init", "--", "/bin/sh", "/app/heroku-entrypoint.sh"]
CMD ["node", "server/server.js"]
