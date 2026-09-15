#!/bin/sh
# Container entrypoint.
#
# Migrations run before the app starts, and a failure here stops the container
# rather than letting it serve against a schema it does not match. `alembic
# upgrade head` is idempotent, so every replica can run it safely.
set -e

echo "Applying database migrations..."
alembic upgrade head

echo "Starting API..."
exec "$@"
