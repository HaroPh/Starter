#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

echo "Starting the exhibition sales CRM."
echo "  The first run builds the image and imports the archive; later runs reuse both."
echo "  The app is at http://localhost:3000 as soon as the port binds; the archive"
echo "  finishes loading shortly after, when you see 'import completed' in the logs."
echo

docker compose \
  --project-directory "$SCRIPT_DIR" \
  --file "$SCRIPT_DIR/compose.yml" \
  up --build --remove-orphans
