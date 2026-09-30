#!/bin/sh
set -eu

d2_binary=${1:-d2}
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# Keep make doctor aligned with the version and checksums used by CI and Docker.
. "$script_dir/../infra/d2.lock"

problem() {
    if [ "${CI:-}" = "true" ]; then
        printf 'ERREUR: %s\n' "$1" >&2
        exit 1
    fi
    printf 'AVERTISSEMENT: %s\n' "$1" >&2
    exit 0
}

if ! d2_path=$(command -v "$d2_binary"); then
    problem "D2 $D2_VERSION est absent (binaire demandé : $d2_binary)."
fi
if ! reported=$("$d2_path" --version 2>/dev/null); then
    problem "D2 est présent mais sa commande --version a échoué."
fi
reported=${reported#v}
if [ "$reported" != "$D2_VERSION" ]; then
    problem "D2 $D2_VERSION est requis, version trouvée : $reported."
fi

printf 'OK: D2 %s est disponible.\n' "$D2_VERSION"
