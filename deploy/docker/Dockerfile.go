# fep-gateway, outstation-sim, pki, mtlsproxy (Go, standard library only). ARG CMD picks the binary.
FROM golang:1.23 AS build
ARG CMD=fep
WORKDIR /src
COPY services/fep-gateway .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/app ./cmd/${CMD}
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/app /app
USER nonroot
ENTRYPOINT ["/app"]
