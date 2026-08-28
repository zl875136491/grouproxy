SHELL := /bin/sh

BINARY := dist/grouproxy-monitor-linux-amd64
GOFLAGS := -trimpath -buildvcs=false

.PHONY: test build dist clean FORCE

FORCE:

test:
	go test ./...

build: FORCE
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build $(GOFLAGS) -o $(BINARY) ./cmd/monitor

dist: test build FORCE
	chmod 0755 $(BINARY)
	sha256sum $(BINARY) > dist/SHA256SUMS

clean:
	go clean
