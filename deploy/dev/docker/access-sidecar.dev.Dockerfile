# DEVELOPMENT_ONLY build of anvilkit-job-access-sidecar against the contracts
# checkout of this parent repository (build context: the parent root). The
# sidecar's own Dockerfile (jobs/shared/access-sidecar/Dockerfile) builds
# from its repository alone against the published contracts module; until
# a contracts release that carries ExecutionService.GetInstance is
# published, that build cannot succeed, and this file substitutes the
# checkout through a replace directive that exists only inside this build.
FROM golang:1.27.0-alpine@sha256:4c9fe60190a2a3350ddc51de80d0224b8a6698d12bdfc999fee45ea9d6c46dbc AS build
ARG GOPROXY=https://proxy.golang.org,direct
ENV GOWORK=off CGO_ENABLED=0 GOPROXY=$GOPROXY
WORKDIR /src
COPY contracts/go /src/contracts/go
COPY jobs/shared/access-sidecar /src/sidecar
WORKDIR /src/sidecar
RUN go mod edit -replace github.com/ancyloce/anvilkit-agent-contracts/go=/src/contracts/go \
 && GOFLAGS=-mod=mod go mod download \
 && GOFLAGS=-mod=mod go build -trimpath -ldflags="-s -w" -o /out/anvilkit-job-access-sidecar ./cmd/anvilkit-job-access-sidecar

FROM alpine:3.24@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b
RUN addgroup -g 10002 sidecar && adduser -D -H -u 10002 -G sidecar -s /sbin/nologin sidecar \
 && mkdir -p /etc/anvilkit/anvilkit-job-access-sidecar /run/anvilkit
COPY --from=build /out/anvilkit-job-access-sidecar /usr/local/bin/anvilkit-job-access-sidecar
COPY --chmod=0644 jobs/shared/access-sidecar/config.yaml /etc/anvilkit/anvilkit-job-access-sidecar/config.yaml
ENV ANVILKIT_SIDECAR_CONFIG=/etc/anvilkit/anvilkit-job-access-sidecar/config.yaml
USER 10002:10002
ENTRYPOINT ["/usr/local/bin/anvilkit-job-access-sidecar"]
