# anvilkit-migration (the parent's migration Job of the knowledge and mcp
# schemas; Control's schema migrates from Control's own image). Built from this
# directory alone, without the workspace:
#   docker build -t anvilkit-migration .
# One run applies one domain's goose migrations under its migrator identity:
#   anvilkit-migration -domain knowledge|mcp (the DSN from ANVILKIT_MIGRATION_DSN
#   or the mounted file ANVILKIT_MIGRATION_DSN_FILE names).
FROM golang:1.27.4-alpine@sha256:70bd7ddea8b6cbe5d6943cde279d6b67feb10cc924fb9100d1d8757c1575b280 AS build
ARG GOPROXY=https://proxy.golang.org,direct
WORKDIR /src
ENV GOWORK=off GOFLAGS=-mod=readonly CGO_ENABLED=0
COPY go.mod go.sum ./
RUN go mod download
COPY cmd ./cmd
COPY internal ./internal
RUN go build -trimpath -ldflags="-s -w" -o /out/anvilkit-migration ./cmd/anvilkit-migration

FROM alpine:3.24@sha256:3f4d6b32e26703c6082839234f43769d5046b3ac7c910033911d1e71d8cd630a
# Alpine zlib CVE-2026-4738: the base ships 1.3.2-r1 and v3.24/main serves
# the fixed 1.4-r0. A floor, not a pin: the build fails while the index
# lacks it, and drop this step once the alpine:3.24 pin ships >= 1.4-r0.
# OpenSSL (libcrypto3, libssl3) is held at the 2026-11-23 scan's fixed
# 3.5.9-r1 the same way.
RUN apk add --no-cache 'zlib>=1.4-r0' 'libcrypto3>=3.5.9-r1' 'libssl3>=3.5.9-r1'
COPY --from=build /out/anvilkit-migration /usr/local/bin/anvilkit-migration
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/anvilkit-migration"]
