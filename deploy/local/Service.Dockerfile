FROM golang:1.27.2-alpine@sha256:f92b6ef800e499660581efdabdf25d9d817a9d124eaf900924f0504e7e27e12d AS build
WORKDIR /src
ENV GOWORK=off
COPY go.mod go.sum ./
RUN go mod download
COPY cmd ./cmd
COPY internal ./internal
ARG SERVICE_BINARY
RUN CGO_ENABLED=0 go build -mod=readonly -trimpath -ldflags="-s -w" \
    -o /out/${SERVICE_BINARY} ./cmd/${SERVICE_BINARY}

FROM alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
RUN mkdir -p /var/lib/anvilkit/intake /var/lib/anvilkit/artifacts && chown 65532:65532 /var/lib/anvilkit/intake /var/lib/anvilkit/artifacts
COPY --from=build /out/ /usr/local/bin/
USER 65532:65532
