#!/bin/sh
set -eu

install_dir=${1:?usage: install-typst-fonts.sh INSTALL_DIR}
install_parent=$(dirname -- "$install_dir")
install_name=$(basename -- "$install_dir")
case "$install_name" in
    ""|.|..)
        echo "font bundle install directory must name a dedicated directory" >&2
        exit 1
        ;;
esac
mkdir -p "$install_parent"
install_parent=$(CDPATH= cd -- "$install_parent" && pwd)
install_dir="$install_parent/$install_name"
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
source_lock=${TYPST_FONTS_SOURCE_LOCK_FILE:-"$script_dir/../infra/typst-fonts-sources.lock"}
font_lock=${TYPST_FONTS_LOCK_FILE:-"$script_dir/../infra/typst-fonts.lock"}
. "$source_lock"

build_dir=$(mktemp -d)
trap 'rm -rf "$build_dir"' EXIT HUP INT TERM

cascadia_archive="$build_dir/cascadia-code.zip"
curl --fail --location --silent --show-error \
    "$CASCADIA_CODE_ARCHIVE_URL" --output "$cascadia_archive"
if ! printf '%s  %s\n' "$CASCADIA_CODE_ARCHIVE_SHA256" "$cascadia_archive" \
    | sha256sum --check --status; then
    echo "checksum verification failed for Cascadia Code archive" >&2
    exit 1
fi
mkdir "$build_dir/cascadia"
unzip -q "$cascadia_archive" -d "$build_dir/cascadia"

hanken_archive="$build_dir/hanken-grotesk.tar.gz"
curl --fail --location --silent --show-error \
    "$HANKEN_GROTESK_ARCHIVE_URL" --output "$hanken_archive"
if ! printf '%s  %s\n' "$HANKEN_GROTESK_ARCHIVE_SHA256" "$hanken_archive" \
    | sha256sum --check --status; then
    echo "checksum verification failed for Hanken Grotesk archive" >&2
    exit 1
fi
mkdir "$build_dir/hanken"
tar -xzf "$hanken_archive" --strip-components=1 -C "$build_dir/hanken"

cascadia_root="$build_dir/cascadia/ttf"
hanken_root="$build_dir/hanken/fonts/ttf"
font_paths_file="$build_dir/font-paths"
staged_install="$build_dir/install"
awk '
    /"files"[[:space:]]*:/ { in_files = 1; next }
    in_files && /^[[:space:]]*]/ { exit }
    in_files {
        path = $0
        sub(/^[[:space:]]*"/, "", path)
        sub(/",[[:space:]]*$/, "", path)
        sub(/"[[:space:]]*$/, "", path)
        if (path != "") print path
    }
' "$font_lock" > "$font_paths_file"
if [ ! -s "$font_paths_file" ]; then
    echo "Typst font lock contains no files: $font_lock" >&2
    exit 1
fi

mkdir -p "$staged_install"
while IFS= read -r relative_path || [ -n "$relative_path" ]; do
    case "/$relative_path/" in
        *"/../"*)
            echo "Typst font lock path escapes the bundle root: $relative_path" >&2
            exit 1
            ;;
    esac
    case "$relative_path" in
        HankenGrotesk/*)
            source_path="$hanken_root/${relative_path#HankenGrotesk/}"
            ;;
        CascadiaCode/ttf/*)
            source_path="$cascadia_root/${relative_path#CascadiaCode/ttf/}"
            ;;
        *)
            echo "unsupported path in Typst font lock: $relative_path" >&2
            exit 1
            ;;
    esac
    if [ ! -f "$source_path" ]; then
        echo "upstream font archive is missing manifest-listed file: $relative_path" >&2
        exit 1
    fi

    destination_path="$staged_install/$relative_path"
    mkdir -p "$(dirname -- "$destination_path")"
    if ! install -m 0644 "$source_path" "$destination_path"; then
        echo "unable to install manifest-listed font: $relative_path" >&2
        exit 1
    fi
    if [ ! -s "$destination_path" ]; then
        echo "installed font is missing or empty: $relative_path" >&2
        exit 1
    fi
done < "$font_paths_file"

if [ -e "$install_dir" ] || [ -L "$install_dir" ]; then
    rm -rf "$install_dir"
fi
mv "$staged_install" "$install_dir"
