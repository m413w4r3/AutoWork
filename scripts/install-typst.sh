#!/bin/sh
set -eu

install_dir=${1:?usage: install-typst.sh INSTALL_DIR}
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
lock_file=${TYPST_LOCK_FILE:-"$script_dir/../infra/typst.lock"}
# The lock file is the shared version and checksum source for Docker and CI.
. "$lock_file"

case "${TARGETARCH:-$(uname -m)}" in
    amd64|x86_64)
        typst_triple=x86_64
        typst_sha256=$TYPST_SHA256_AMD64
        ;;
    arm64|aarch64)
        typst_triple=aarch64
        typst_sha256=$TYPST_SHA256_ARM64
        ;;
    *)
        echo "unsupported Typst architecture: ${TARGETARCH:-$(uname -m)}" >&2
        exit 1
        ;;
esac

build_dir=$(mktemp -d)
trap 'rm -rf "$build_dir"' EXIT HUP INT TERM
archive="$build_dir/typst-$typst_triple-unknown-linux-musl.tar.xz"
curl --fail --location --silent --show-error \
    "https://github.com/typst/typst/releases/download/v$TYPST_VERSION/typst-$typst_triple-unknown-linux-musl.tar.xz" \
    --output "$archive"
printf '%s  %s\n' "$typst_sha256" "$archive" | sha256sum --check --status
mkdir "$build_dir/extract"
tar -xJf "$archive" --strip-components=1 -C "$build_dir/extract"
mkdir -p "$install_dir"
install -m 0755 "$build_dir/extract/typst" "$install_dir/typst"
reported_version=$("$install_dir/typst" --version)
case "$reported_version" in
    "typst $TYPST_VERSION "*) ;;
    *)
        echo "installed Typst version did not match $TYPST_VERSION: $reported_version" >&2
        exit 1
        ;;
esac
