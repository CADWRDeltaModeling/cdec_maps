#!/bin/bash
# Entry point for the scheduled cache-build job (see deploy_cache_job.ps1).
# Runs on the shared data share and exits non-zero on failure so the job is
# marked failed. The web app (run_server.sh) only serves; it never builds.
# Extra arguments are passed to cdec_maps.cdec_cache_build (e.g. --dbase-dir).
set -eu
cd "$(dirname "$0")"
git config --global --add safe.directory "$(pwd)"
SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 pip install --no-deps .
exec python -m cdec_maps.cdec_cache_build "$@"
