#!/bin/sh
set -eu

typst_binary=${1:-typst}
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# Keep make doctor aligned with the version and checksums used by CI and Docker.
. "$script_dir/../infra/typst.lock"

problem() {
    if [ "${CI:-}" = "true" ]; then
        printf 'ERREUR: %s\n' "$1" >&2
        exit 1
    fi
    printf 'AVERTISSEMENT: %s\n' "$1" >&2
    exit 0
}

if ! typst_path=$(command -v "$typst_binary"); then
    problem "Typst $TYPST_VERSION est absent (binaire demandé : $typst_binary)."
fi
if ! reported=$("$typst_path" --version 2>/dev/null); then
    problem "Typst est présent mais sa commande --version a échoué."
fi
case "$reported" in
    "typst $TYPST_VERSION "*) ;;
    *)
        problem "Typst $TYPST_VERSION est requis, version trouvée : $reported."
        ;;
esac

printf 'OK: Typst %s est disponible.\n' "$TYPST_VERSION"
