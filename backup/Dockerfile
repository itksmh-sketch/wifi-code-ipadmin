FROM postgres:15-alpine

RUN apk add --no-cache curl tzdata
RUN curl -fsSL -o /usr/local/bin/supercronic "https://github.com/aptible/supercronic/releases/download/v0.2.29/supercronic-linux-amd64" \
    && chmod +x /usr/local/bin/supercronic

WORKDIR /app
COPY backup.sh /app/backup.sh
COPY crontab /app/crontab
RUN chmod +x /app/backup.sh

CMD ["/usr/local/bin/supercronic", "/app/crontab"]
