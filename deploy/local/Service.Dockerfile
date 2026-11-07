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

FROM alpine:3.24@sha256:3f4d6b32e26703c6082839234f43769d5046b3ac7c910033911d1e71d8cd630a
# Alpine zlib CVE-2026-4738: the base ships 1.3.2-r1 and v3.24/main serves
# the fixed 1.4-r0. A floor, not a pin: the build fails while the index
# lacks it, and drop this step once the alpine:3.24 pin ships >= 1.4-r0.
RUN apk add --no-cache 'zlib>=1.4-r0'
RUN mkdir -p /var/lib/anvilkit/intake /var/lib/anvilkit/artifacts && chown 65532:65532 /var/lib/anvilkit/intake /var/lib/anvilkit/artifacts
COPY --from=build /out/ /usr/local/bin/
USER 65532:65532
