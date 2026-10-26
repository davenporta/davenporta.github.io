#!/usr/bin/env bash
# Generate the prime256v1 (P-256) key pair Tesla requires for Fleet API + virtual keys.
# Source: https://developer.tesla.com/docs/fleet-api/getting-started/what-is-fleet-api (step 3)
# The private key never leaves this machine; only the public key is published.
set -euo pipefail
DIR="${1:-$(dirname "$0")/../keys}"
mkdir -p "$DIR"; chmod 700 "$DIR"
if [[ -e "$DIR/private-key.pem" ]]; then
  echo "Refusing to overwrite existing $DIR/private-key.pem (it is paired with your car)." >&2
  exit 1
fi
umask 077
openssl ecparam -name prime256v1 -genkey -noout -out "$DIR/private-key.pem"
openssl ec -in "$DIR/private-key.pem" -pubout -out "$DIR/public-key.pem" 2>/dev/null
chmod 600 "$DIR/private-key.pem"; chmod 644 "$DIR/public-key.pem"
echo "Private key: $DIR/private-key.pem (0600, keep secret, never commit)"
echo "Public key:  $DIR/public-key.pem -> publish at https://<domain>/.well-known/appspecific/com.tesla.3p.public-key.pem"
