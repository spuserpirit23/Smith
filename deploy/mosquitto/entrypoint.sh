#!/bin/sh
# Start Mosquitto once the backend has written the password file, then reload the broker
# (SIGHUP) whenever that file changes, e.g. after a device is provisioned or disabled.
#
# The backend writes /mosquitto/auth/passwd on a shared volume. Mosquitto 2.x wants its auth
# files owned by the mosquitto user with no group/world access, so we install private copies
# under /mosquitto/run/ and point the config there.
set -eu
SRC=/mosquitto/auth/passwd
RUN=/mosquitto/run

install_auth() {
  mkdir -p "$RUN"
  cp "$SRC" "$RUN/passwd.tmp" && mv "$RUN/passwd.tmp" "$RUN/passwd"
  cp /mosquitto/config/acl "$RUN/acl"
  chown -R mosquitto:mosquitto "$RUN" /mosquitto/data
  chmod 0700 "$RUN"
  chmod 0600 "$RUN/passwd" "$RUN/acl"
}

until [ -s "$SRC" ]; do
  echo "entrypoint: waiting for backend to write $SRC"
  sleep 2
done
install_auth

mosquitto -c /mosquitto/config/mosquitto.conf &
PID=$!
trap 'kill -TERM $PID 2>/dev/null; wait $PID; exit 0' TERM INT

last=$(md5sum "$SRC" | cut -d" " -f1)
while kill -0 "$PID" 2>/dev/null; do
  sleep 3
  cur=$(md5sum "$SRC" 2>/dev/null | cut -d" " -f1 || echo "$last")
  if [ "$cur" != "$last" ]; then
    last=$cur
    install_auth
    kill -HUP "$PID" && echo "entrypoint: password file changed, broker reloaded"
  fi
done
wait "$PID"
