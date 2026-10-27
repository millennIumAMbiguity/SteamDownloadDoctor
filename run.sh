#!/usr/bin/env sh
# Runs the Steam corruption review. Pass a log path as an optional argument.
cd "$(dirname "$0")" || exit 1

for py in python3 python; do
    if command -v "$py" >/dev/null 2>&1; then
        exec "$py" steam_download_doctor.py "$@"
    fi
done

echo "Python 3 was not found. Install it with your package manager (e.g. sudo apt install python3)."
exit 1
