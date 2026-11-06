#!/bin/sh
# The suite on every supported Python, and on the lowest supported dependencies, in Docker.
#   tests/docker/run.sh            # 3.10 to 3.14, latest dependencies
#   tests/docker/run.sh lowest     # 3.10 with httpx 0.27 and Pillow 10.3
set -eu
cd "$(dirname "$0")/../.."
run() {
    image="python:$1-slim"
    shift
    echo "== $image $*"
    docker run --rm -v "$PWD:/src:ro" "$image" sh -ec "
        cp -r /src /work && cd /work && rm -rf .venv
        pip install -q --root-user-action=ignore --disable-pip-version-check $* '.[fastapi,django,flask]' pytest anyio python-multipart
        python -m pytest -q -p no:cacheprovider
    "
}
if [ "${1:-}" = "lowest" ]; then
    run 3.10 "httpx==0.27.0" "pillow==10.3.0" "django==4.2.*" "flask==2.3.*" "fastapi==0.115.*"
else
    for version in 3.10 3.11 3.12 3.13 3.14; do run "$version"; done
fi
