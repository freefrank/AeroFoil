#!/bin/bash

# Retrieve from Environment variables, or use 1000 as default
gid=${PGID:-1000}
uid=${PUID:-1000}

# Ensure group exists for requested GID.
if ! getent group "${gid}" >/dev/null 2>&1; then
  groupadd -g "${gid}" aerofoil
fi
GROUP=$(getent group "${gid}" | cut -d ":" -f 1)

# Ensure user exists for requested UID and belongs to that group.
if ! getent passwd "${uid}" >/dev/null 2>&1; then
  useradd -u "${uid}" -g "${GROUP}" -M -s /usr/sbin/nologin aerofoil
fi

chown -R ${uid}:${gid} /app
chown -R ${uid}:${gid} /root

echo "Starting AeroFoil"

exec sudo -E -u "#${uid}" python /app/app.py

