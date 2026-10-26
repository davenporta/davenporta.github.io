#!/usr/bin/env bash
# Self-signed localhost TLS cert for tesla-http-proxy (TLS is mandatory for the proxy).
# Source: https://github.com/teslamotors/vehicle-command (README, "Using the HTTP proxy")
set -euo pipefail
DIR="${1:-$(dirname "$0")/../proxy-config}"
mkdir -p "$DIR"; chmod 700 "$DIR"; umask 077
openssl req -x509 -nodes -newkey ec -pkeyopt ec_paramgen_curve:secp384r1 -pkeyopt ec_param_enc:named_curve \
  -subj '/CN=localhost' -keyout "$DIR/tls-key.pem" -out "$DIR/tls-cert.pem" -sha256 -days 3650 \
  -addext "extendedKeyUsage = serverAuth" -addext "keyUsage = digitalSignature, keyCertSign, keyAgreement" \
  -addext "subjectAltName = DNS:localhost,IP:127.0.0.1"
chmod 644 "$DIR/tls-cert.pem"
echo "TLS cert: $DIR/tls-cert.pem  (set TESLA_PROXY_CA_CERT to this path)"
