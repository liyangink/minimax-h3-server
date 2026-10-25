#!/bin/sh
set -eu

projectDir=$(CDPATH= cd "$(dirname "$0")" && pwd -P)

if [ ! -x "$projectDir/.venv/bin/python" ]; then
  echo "Missing Python virtual environment: $projectDir/.venv" >&2
  exit 1
fi
if [ ! -f "$projectDir/config.json" ]; then
  echo "Missing configuration file: $projectDir/config.json" >&2
  exit 1
fi
for program in ffmpeg ffprobe; do
  if ! command -v "$program" >/dev/null 2>&1; then
    echo "Missing required program on PATH: $program" >&2
    exit 1
  fi
done

cd "$projectDir"
exec "$projectDir/.venv/bin/python" -m server.main --config "$projectDir/config.json"
