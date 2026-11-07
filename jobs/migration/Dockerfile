# anvilkit-migration (the parent's migration Job of the knowledge and mcp
# schemas; Control's schema migrates from Control's own image). Built from this
# directory alone, without the workspace:
#   docker build -t anvilkit-migration .
# One run applies one domain's goose migrations under its migrator identity:
#   anvilkit-migration -domain knowledge|mcp (the DSN from ANVILKIT_MIGRATION_DSN
#   or the mounted file ANVILKIT_MIGRATION_DSN_FILE names).
FROM golang:1.27.3-alpine@sha256:b8c9b82affbbb82a020bbcf998dad2ba9516944ac7e883524642a481a9505e9c AS build
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
RUN apk add --no-cache 'zlib>=1.4-r0'
COPY --from=build /out/anvilkit-migration /usr/local/bin/anvilkit-migration
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/anvilkit-migration"]
