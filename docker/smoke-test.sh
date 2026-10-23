#!/bin/sh
# Smoke-test one architecture of a built image, straight from the OCI archive
# that will later be published byte-for-byte. Used by .github/workflows/image.yml.
#
#   docker/smoke-test.sh <oci-archive.tar> <amd64|arm64> <host-port>
#
# Boots the container with an EMPTY /config and a non-default PUID/PGID, then
# checks local health, privilege drop, file ownership, the setup claim code,
# and that a restart keeps state.
set -eu

ARCHIVE="$1"
ARCH="$2"
PORT="$3"
NAME="wp-smoke-$ARCH"
IMAGE="wishpanel-smoke:$ARCH"
URL="http://127.0.0.1:$PORT"
UIDGID="1001:1001"
CONFIG="$(mktemp -d)"

fail() { echo "SMOKE FAIL [$ARCH]: $*" >&2; docker logs "$NAME" 2>&1 | tail -60 >&2 || true; exit 1; }
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== [$ARCH] load image from archive"
skopeo copy --quiet --override-os linux --override-arch "$ARCH" \
  "oci-archive:$ARCHIVE" "docker-daemon:$IMAGE"
got_arch="$(docker image inspect -f '{{.Architecture}}' "$IMAGE")"
[ "$got_arch" = "$ARCH" ] || fail "image architecture is $got_arch"

wait_healthy() {
  i=0
  while [ $i -lt 90 ]; do
    state="$(docker inspect -f '{{.State.Health.Status}}' "$NAME" 2>/dev/null || echo missing)"
    [ "$state" = "healthy" ] && return 0
    [ "$state" = "unhealthy" ] && fail "container became unhealthy"
    i=$((i + 1)); sleep 2
  done
  fail "not healthy after 180s (last: $state)"
}

echo "== [$ARCH] boot with empty /config"
docker run -d --name "$NAME" --platform "linux/$ARCH" -p "127.0.0.1:$PORT:8080" \
  -e PUID=1001 -e PGID=1001 -e TZ=America/Los_Angeles -v "$CONFIG:/config" "$IMAGE" >/dev/null
wait_healthy

echo "== [$ARCH] local health only"
health="$(curl -fsS "$URL/health")" || fail "/health not 200"
echo "$health"
echo "$health" | grep -q '"status":"ok"' || fail "/health status not ok"
echo "$health" | grep -q '"database"' || fail "/health missing database check"
if echo "$health" | grep -qi kavita; then fail "/health must not include external services"; fi

echo "== [$ARCH] UI served"
curl -fsS "$URL/" | grep -q WishPanel || fail "index page"
curl -fsS -o /dev/null "$URL/static/vendor/alpine-3.17.5.min.js" || fail "vendored Alpine missing"

echo "== [$ARCH] runs as PUID, not root"
docker exec "$NAME" grep -Eq '^Uid:\s+1001\s' /proc/1/status || fail "PID 1 is not uid 1001"

echo "== [$ARCH] /config ownership and modes"
for f in secret.key setup-code.txt wishpanel.db; do
  owner="$(docker exec "$NAME" stat -c %u:%g "/config/$f")" || fail "missing /config/$f"
  [ "$owner" = "$UIDGID" ] || fail "/config/$f owned by $owner"
done
for f in secret.key setup-code.txt; do
  mode="$(docker exec "$NAME" stat -c %a "/config/$f")"
  [ "$mode" = "600" ] || fail "/config/$f mode $mode"
done

echo "== [$ARCH] setup claim code"
CODE="$(docker exec "$NAME" cat /config/setup-code.txt | tr -d '\r\n')"
echo "$CODE" | grep -Eq '^[A-Z2-9]{4}(-[A-Z2-9]{4}){3}$' || fail "bad code format"
docker logs "$NAME" 2>&1 | grep -q "$CODE" || fail "code not printed to container log"

post() { curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d "$2" "$URL$1"; }
[ "$(post /api/setup/claim '{"code":"AAAA-BBBB-CCCC-DDDD"}')" = 403 ] || fail "wrong code accepted (claim)"
[ "$(post /api/setup/kavita/test '{"code":"AAAA-BBBB-CCCC-DDDD","url":"http://127.0.0.1:9"}')" = 403 ] \
  || fail "wrong code accepted (kavita/test)"
[ "$(post /api/setup/kavita/test '{"url":"http://127.0.0.1:9"}')" = 422 ] || fail "missing code accepted"
[ "$(post /api/setup/claim "{\"code\":\"$CODE\"}")" = 200 ] || fail "right code rejected"

echo "== [$ARCH] restart keeps state"
KEY_BEFORE="$(docker exec "$NAME" sha256sum /config/secret.key)"
docker restart "$NAME" >/dev/null
wait_healthy
[ "$(docker exec "$NAME" sha256sum /config/secret.key)" = "$KEY_BEFORE" ] || fail "secret.key changed"
[ "$(docker exec "$NAME" cat /config/setup-code.txt | tr -d '\r\n')" = "$CODE" ] || fail "code changed"

echo "== [$ARCH] PASS"
