#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE_NAME="${IMAGE_NAME:-sojiroh/cb-stream-saver}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"

if [ "$#" -eq 0 ]; then
    TAGS=("latest")
else
    TAGS=("$@")
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "Error: docker is required but was not found on PATH." >&2
    exit 1
fi

if ! docker buildx version >/dev/null 2>&1; then
    echo "Error: docker buildx is required. Install or enable Docker Buildx first." >&2
    exit 1
fi

tag_args=()
for tag in "${TAGS[@]}"; do
    tag_args+=("-t" "${IMAGE_NAME}:${tag}")
done

echo "Publishing ${IMAGE_NAME} for ${PLATFORMS}"
printf 'Tags:'
for tag in "${TAGS[@]}"; do
    printf ' %s' "${tag}"
done
printf '\n'

docker buildx build \
    --platform "${PLATFORMS}" \
    "${tag_args[@]}" \
    --push \
    .
