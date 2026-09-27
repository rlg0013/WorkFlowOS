FROM golang:1.21-alpine AS builder

WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=1 go build -o server ./cmd/server

FROM alpine:3.18
RUN apk add --no-cache sqlite-libs gcc musl-dev
WORKDIR /app
COPY --from=builder /app/server .
COPY config.yaml .
EXPOSE 8080
CMD ["./server"]