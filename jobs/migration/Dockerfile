# anvilkit-migration (the parent's migration Job of the knowledge and mcp
# schemas; Control's schema migrates from Control's own image). Built from this
# directory alone, without the workspace:
#   docker build -t anvilkit-migration .
# One run applies one domain's goose migrations under its migrator identity:
#   anvilkit-migration -domain knowledge|mcp (the DSN from ANVILKIT_MIGRATION_DSN).
FROM golang:1.27.0-alpine@sha256:4c9fe60190a2a3350ddc51de80d0224b8a6698d12bdfc999fee45ea9d6c46dbc AS build
ARG GOPROXY=https://proxy.golang.org,direct
WORKDIR /src
ENV GOWORK=off GOFLAGS=-mod=readonly CGO_ENABLED=0
COPY go.mod go.sum ./
RUN go mod download
COPY cmd ./cmd
COPY internal ./internal
RUN go build -trimpath -ldflags="-s -w" -o /out/anvilkit-migration ./cmd/anvilkit-migration

FROM alpine:3.24@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b
COPY --from=build /out/anvilkit-migration /usr/local/bin/anvilkit-migration
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/anvilkit-migration"]
