#!/bin/sh
set -eu

install_dir=${1:?usage: install-d2.sh INSTALL_DIR}
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
lock_file=${D2_LOCK_FILE:-"$script_dir/../infra/d2.lock"}
# The lock file is the shared version and checksum source for Docker and CI.
. "$lock_file"

case "${TARGETARCH:-$(uname -m)}" in
    amd64|x86_64)
        d2_arch=amd64
        d2_sha256=$D2_SHA256_AMD64
        ;;
    arm64|aarch64)
        d2_arch=arm64
        d2_sha256=$D2_SHA256_ARM64
        ;;
    *)
        echo "unsupported D2 architecture: ${TARGETARCH:-$(uname -m)}" >&2
        exit 1
        ;;
esac

build_dir=$(mktemp -d)
trap 'rm -rf "$build_dir"' EXIT HUP INT TERM
archive="$build_dir/d2-v$D2_VERSION-linux-$d2_arch.tar.gz"
curl --fail --location --silent --show-error \
    "https://github.com/terrastruct/d2/releases/download/v$D2_VERSION/d2-v$D2_VERSION-linux-$d2_arch.tar.gz" \
    --output "$archive"
printf '%s  %s\n' "$d2_sha256" "$archive" | sha256sum --check --status
mkdir "$build_dir/extract"
tar -xzf "$archive" --strip-components=1 -C "$build_dir/extract"
mkdir -p "$install_dir"
install -m 0755 "$build_dir/extract/bin/d2" "$install_dir/d2"
test "$("$install_dir/d2" --version)" = "v$D2_VERSION"
