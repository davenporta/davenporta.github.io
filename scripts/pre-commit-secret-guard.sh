#!/bin/sh
# Block commits containing secrets or private keys.
fail=0
for f in $(git diff --cached --name-only --diff-filter=ACM); do
  case "$f" in
    *.env|.env*|*tokens.json|*private*key*|*.key|keys/*|*/keys/*) echo "BLOCKED: secret-looking file $f"; fail=1;;
  esac
done
if git diff --cached -U0 | grep -E '^\+' | grep -E -i 'BEGIN [A-Z ]*PRIVATE KEY|ta-secret\.|gh[opsu]_[A-Za-z0-9]{20,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}|refresh_token"?\s*[:=]\s*"[^"]{20,}' >/dev/null; then
  echo "BLOCKED: staged changes contain a private key or token"; fail=1
fi
[ -n "$TESLA_CLIENT_ID" ] && git diff --cached | grep -F -q "$TESLA_CLIENT_ID" && { echo "BLOCKED: client ID in staged changes"; fail=1; }
[ -n "$TESLA_CLIENT_SECRET" ] && git diff --cached | grep -F -q "$TESLA_CLIENT_SECRET" && { echo "BLOCKED: client secret in staged changes"; fail=1; }
[ -n "$ICLOUD_APPLE_ID" ] && git diff --cached | grep -F -q "$ICLOUD_APPLE_ID" && { echo "BLOCKED: iCloud Apple ID in staged changes"; fail=1; }
[ -n "$ICLOUD_APP_PASSWORD" ] && git diff --cached | grep -F -q "$ICLOUD_APP_PASSWORD" && { echo "BLOCKED: iCloud app password in staged changes"; fail=1; }
exit $fail
