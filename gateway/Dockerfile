# TsukiyoHub LLM 路由网关（Go，零第三方依赖）
FROM golang:1.22-alpine AS build

WORKDIR /src
# 只有标准库，无需 go mod download
COPY gateway/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -X main.Version=0.1.0" \
    -o /out/tsukiyo-gateway ./cmd/tsukiyo-gateway

FROM alpine:3.20 AS run

RUN apk add --no-cache ca-certificates tzdata && adduser -D -u 10001 gateway
WORKDIR /app
COPY --from=build /out/tsukiyo-gateway /app/tsukiyo-gateway
COPY gateway/config.example.json /app/config.json
USER gateway

EXPOSE 8081 8082
ENTRYPOINT ["/app/tsukiyo-gateway"]
CMD ["-config", "/app/config.json"]