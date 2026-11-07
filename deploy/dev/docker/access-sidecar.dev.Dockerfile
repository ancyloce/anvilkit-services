# DEVELOPMENT_ONLY build of anvilkit-job-access-sidecar against the contracts
# checkout of this parent repository (build context: the parent root). The
# sidecar's own Dockerfile (jobs/shared/access-sidecar/Dockerfile) builds
# from its repository alone against the published contracts module, and
# deploy/dev/images.sh uses that build; this file substitutes the checkout
# through a replace directive that exists only inside this build (contracts
# changes not yet published), and nothing in this repository invokes it.
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

FROM alpine:3.24@sha256:3f4d6b32e26703c6082839234f43769d5046b3ac7c910033911d1e71d8cd630a
# Alpine zlib CVE-2026-4738: the base ships 1.3.2-r1 and v3.24/main serves
# the fixed 1.4-r0. A floor, not a pin: the build fails while the index
# lacks it, and drop this step once the alpine:3.24 pin ships >= 1.4-r0.
RUN apk add --no-cache 'zlib>=1.4-r0'
RUN addgroup -g 10002 sidecar && adduser -D -H -u 10002 -G sidecar -s /sbin/nologin sidecar \
 && mkdir -p /etc/anvilkit/anvilkit-job-access-sidecar
COPY --from=build /out/anvilkit-job-access-sidecar /usr/local/bin/anvilkit-job-access-sidecar
COPY --chmod=0644 jobs/shared/access-sidecar/config.yaml /etc/anvilkit/anvilkit-job-access-sidecar/config.yaml
ENV ANVILKIT_SIDECAR_CONFIG=/etc/anvilkit/anvilkit-job-access-sidecar/config.yaml
USER 10002:10002
ENTRYPOINT ["/usr/local/bin/anvilkit-job-access-sidecar"]
