# syntax=docker/dockerfile:1.7

# --- build stage ------------------------------------------------------------
FROM golang:1.25-bookworm AS build
WORKDIR /src

# go-sqlite3 needs cgo, so we cannot ship FROM scratch. We can still go
# distroless/base in the runtime stage since cgo only needs glibc.
ENV CGO_ENABLED=1

COPY go.mod go.sum ./
RUN go mod download

COPY . .
# -trimpath strips host build paths from the binary; -ldflags="-s -w" drops
# the symbol/DWARF tables (~30% binary size, no debug impact in production).
# -tags sqlite_fts5 compiles the FTS5 module into the embedded SQLite (off by
# default in go-sqlite3); needed for the messages_fts virtual table.
RUN go build -tags "sqlite_fts5" -trimpath -ldflags="-s -w" -o /out/whatsapp-bridge-bin .

# Stage passwd/group + store/ skeleton out for the runtime image (distroless
# has no shell, so all setup must happen here as plain files we COPY).
RUN echo 'app:x:10001:10001::/app:/sbin/nologin' > /tmp/passwd \
 && echo 'app:x:10001:' > /tmp/group \
 && mkdir -p /out/app/store \
 && chown -R 10001:10001 /out/app

# --- runtime stage ----------------------------------------------------------
# distroless/base-debian12 = glibc + ca-certificates only. No shell, no apt,
# nothing to attack. ~22MB base vs ~75MB for debian-slim, and there are no
# package CVEs to track because there are no packages.
FROM gcr.io/distroless/base-debian12

COPY --from=build /tmp/passwd /etc/passwd
COPY --from=build /tmp/group  /etc/group
COPY --from=build --chown=10001:10001 /out/app /app
COPY --from=build --chown=10001:10001 /out/whatsapp-bridge-bin /app/whatsapp-bridge-bin

WORKDIR /app
USER 10001:10001

# REST API port (reached only over the private docker network; not published).
EXPOSE 8080

# No tini: distroless's entrypoint forwards signals to PID 1 cleanly, and the
# Go runtime handles SIGTERM via the default signal handler.
ENTRYPOINT ["/app/whatsapp-bridge-bin"]
