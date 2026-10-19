# DEVELOPMENT_ONLY build of anvilkit-job-access-sidecar against the contracts
# checkout of this parent repository (build context: the parent root). The
# sidecar's own Dockerfile (jobs/shared/access-sidecar/Dockerfile) builds
# from its repository alone against the published contracts module; until
# a contracts release that carries ExecutionService.GetInstance is
# published, that build cannot succeed, and this file substitutes the
# checkout through a replace directive that exists only inside this build.
FROM golang:1.27.2-alpine@sha256:f92b6ef800e499660581efdabdf25d9d817a9d124eaf900924f0504e7e27e12d AS build
ARG GOPROXY=https://proxy.golang.org,direct
ENV GOWORK=off CGO_ENABLED=0 GOPROXY=$GOPROXY
WORKDIR /src
COPY contracts/go /src/contracts/go
COPY jobs/shared/access-sidecar /src/sidecar
WORKDIR /src/sidecar
RUN go mod edit -replace github.com/ancyloce/anvilkit-agent-contracts/go=/src/contracts/go \
 && GOFLAGS=-mod=mod go mod download \
 && GOFLAGS=-mod=mod go build -trimpath -ldflags="-s -w" -o /out/anvilkit-job-access-sidecar ./cmd/anvilkit-job-access-sidecar

FROM alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
RUN addgroup -g 10002 sidecar && adduser -D -H -u 10002 -G sidecar -s /sbin/nologin sidecar \
 && mkdir -p /etc/anvilkit/anvilkit-job-access-sidecar /run/anvilkit
COPY --from=build /out/anvilkit-job-access-sidecar /usr/local/bin/anvilkit-job-access-sidecar
COPY --chmod=0644 jobs/shared/access-sidecar/config.yaml /etc/anvilkit/anvilkit-job-access-sidecar/config.yaml
ENV ANVILKIT_SIDECAR_CONFIG=/etc/anvilkit/anvilkit-job-access-sidecar/config.yaml
USER 10002:10002
ENTRYPOINT ["/usr/local/bin/anvilkit-job-access-sidecar"]
