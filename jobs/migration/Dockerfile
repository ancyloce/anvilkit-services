# anvilkit-migration (the parent's migration Job of the knowledge and mcp
# schemas; Control's schema migrates from Control's own image). Built from this
# directory alone, without the workspace:
#   docker build -t anvilkit-migration .
# One run applies one domain's goose migrations under its migrator identity:
#   anvilkit-migration -domain knowledge|mcp (the DSN from ANVILKIT_MIGRATION_DSN).
FROM golang:1.27.2-alpine@sha256:f92b6ef800e499660581efdabdf25d9d817a9d124eaf900924f0504e7e27e12d AS build
ARG GOPROXY=https://proxy.golang.org,direct
WORKDIR /src
ENV GOWORK=off GOFLAGS=-mod=readonly CGO_ENABLED=0
COPY go.mod go.sum ./
RUN go mod download
COPY cmd ./cmd
COPY internal ./internal
RUN go build -trimpath -ldflags="-s -w" -o /out/anvilkit-migration ./cmd/anvilkit-migration

FROM alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
COPY --from=build /out/anvilkit-migration /usr/local/bin/anvilkit-migration
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/anvilkit-migration"]
