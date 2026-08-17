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

# Recursively chown a tree only when its top-level ownership is wrong (first
# run, or PUID/PGID changed). The mounted config/data trees can hold hundreds
# of thousands of cached media files, and unconditionally walking them delayed
# every container start by minutes. Everything the app writes afterwards is
# created as ${uid}:${gid}, so a correct top level implies a correct tree.
# Set AEROFOIL_FORCE_CHOWN=1 to force the full walk once after manual edits.
ensure_owner() {
  local dir="$1"
  [ -e "${dir}" ] || return 0
  if [ "${AEROFOIL_FORCE_CHOWN:-0}" = "1" ] || [ "$(stat -c '%u:%g' "${dir}")" != "${uid}:${gid}" ]; then
    echo "Fixing ownership of ${dir}..."
    chown -R "${uid}:${gid}" "${dir}"
  fi
}

# Application code from the image is small; chown it unconditionally, but keep
# the potentially huge mounted trees behind the ownership check.
chown "${uid}:${gid}" /app
find /app -mindepth 1 -maxdepth 1 ! -name config ! -name data -exec chown -R "${uid}:${gid}" {} +
ensure_owner /app/config
ensure_owner /app/data
ensure_owner /root

echo "Starting AeroFoil"

exec sudo -E -u "#${uid}" python /app/app.py
