FROM golang:1.27.0-alpine@sha256:4c9fe60190a2a3350ddc51de80d0224b8a6698d12bdfc999fee45ea9d6c46dbc AS build
WORKDIR /src
ENV GOWORK=off
COPY go.mod go.sum ./
RUN go mod download
COPY cmd ./cmd
COPY internal ./internal
ARG SERVICE_BINARY
RUN CGO_ENABLED=0 go build -mod=readonly -trimpath -ldflags="-s -w" \
    -o /out/${SERVICE_BINARY} ./cmd/${SERVICE_BINARY}

FROM alpine:3.24@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b
RUN mkdir -p /var/lib/anvilkit/intake /var/lib/anvilkit/artifacts && chown 65532:65532 /var/lib/anvilkit/intake /var/lib/anvilkit/artifacts
COPY --from=build /out/ /usr/local/bin/
USER 65532:65532
