#!/bin/sh
# First start: seed /data with the example configs so the UI has something to edit.
set -e
mkdir -p /data
for f in profile.example.json:profile.json searches.example.yaml:searches.yaml run_settings.env:run_settings.env api_keys.example.env:api_keys.env; do
  src=/app/${f%%:*}; dst=/data/${f##*:}
  [ -f "$dst" ] || cp "$src" "$dst"
done
[ -f /data/daily_run.sh ] || cp /app/daily_run.sh /data/daily_run.sh
grep -q '^HEADLESS=' /data/run_settings.env || echo 'HEADLESS=1' >> /data/run_settings.env
sed -i 's/^HEADLESS=0/HEADLESS=1/' /data/run_settings.env
exec "$@"
