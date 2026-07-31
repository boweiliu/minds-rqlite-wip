#!/usr/bin/env bash
# env.d unit: the data-store app's non-pip runtime binaries -- rqlited (the
# rqlite server, the storage engine behind the JSON document store) and lt
# (localtunnel, the zero-setup fallback public tunnel). Neither is pip-installable
# and neither is baked into the image, so this unit converges them on boot.
#
# env.d contract: idempotent with a fast satisfied-check -- NO marker files. A
# satisfied unit exits 0 in milliseconds; version stability comes from the pins
# below, never from silently tracking "latest".
set -euo pipefail

# rqlite release pin (github.com/rqlite/rqlite). The linux-amd64 (glibc) build;
# Debian 13 is glibc, not musl.
readonly RQLITE_VERSION="v10.2.7"
readonly RQLITE_SHA256="0b4e8ffbbacc84e421c49915bbd82cc67ac6488e22d4ce58b678fc1219c6a38a"
readonly RQLITE_URL="https://github.com/rqlite/rqlite/releases/download/${RQLITE_VERSION}/rqlite-${RQLITE_VERSION}-linux-amd64.tar.gz"
readonly INSTALL_DIR="/usr/local/bin"

_log() {
    printf '[env.d/data-store] %s\n' "$*"
}

_install_rqlited() {
    if command -v rqlited >/dev/null 2>&1; then
        _log "rqlited present, satisfied"
        return 0
    fi
    _log "installing rqlited ${RQLITE_VERSION}"
    local tmp
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' RETURN
    curl -fsSL "$RQLITE_URL" -o "$tmp/rqlite.tar.gz"
    echo "${RQLITE_SHA256}  $tmp/rqlite.tar.gz" | sha256sum -c -
    tar xzf "$tmp/rqlite.tar.gz" -C "$tmp"
    install -m 0755 "$tmp/rqlite-${RQLITE_VERSION}-linux-amd64/rqlited" "$INSTALL_DIR/rqlited"
    # The rqlite CLI is handy for local inspection of the store.
    install -m 0755 "$tmp/rqlite-${RQLITE_VERSION}-linux-amd64/rqlite" "$INSTALL_DIR/rqlite"
    _log "rqlited installed at $INSTALL_DIR/rqlited"
}

_install_localtunnel() {
    if command -v lt >/dev/null 2>&1; then
        _log "lt present, satisfied"
        return 0
    fi
    _log "installing localtunnel via npm"
    npm install -g localtunnel
    _log "lt installed"
}

_install_rqlited
_install_localtunnel
_log "converged"
