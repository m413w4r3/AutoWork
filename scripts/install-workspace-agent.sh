#!/usr/bin/env bash
# Render and optionally install the systemd --user unit of the AutoWork host
# workspace agent.
#
# Safety contract:
#   - The default mode is a preview: it prints the rendered unit and every
#     command this script would run, and writes nothing.
#   - --dry-run is the explicit form of that preview.
#   - --apply only copies the rendered unit into
#     ${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user. It never enables or starts
#     the unit.
#   - sudo is never executed. The one-time group commands are printed for the
#     operator to run manually.
#   - The workspace tree is never created, modified or deleted.

set -euo pipefail

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
repository_root=$(CDPATH='' cd -- "$script_dir/.." && pwd -P)
unit_template="$repository_root/infra/systemd/autowork-workspace-agent.service"
unit_name="autowork-workspace-agent.service"
group_name="autowork-workspace"
config_home=${XDG_CONFIG_HOME:-${HOME:?HOME must be set}/.config}
unit_dir="$config_home/systemd/user"
unit_target="$unit_dir/$unit_name"

usage() {
    cat <<'EOF'
Usage: install-workspace-agent.sh [--dry-run | --apply]

  (no option)  Preview: print the rendered unit and the commands that would run.
  --dry-run    Same preview, explicitly; no file is written.
  --apply      Write the rendered unit to ${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user.
               The unit is not enabled or started; follow-up commands are printed.
  -h, --help   Show this help.

This script never runs sudo and never touches the workspace tree.
EOF
}

mode=preview
while (($# > 0)); do
    case "$1" in
        --dry-run)
            mode=dry-run
            ;;
        --apply)
            mode=apply
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            printf 'unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [[ ! -f "$unit_template" ]]; then
    printf 'unit template not found: %s\n' "$unit_template" >&2
    exit 1
fi
if ! grep -q '@AUTOWORK_REPOSITORY_ROOT@' "$unit_template"; then
    printf 'unit template has no @AUTOWORK_REPOSITORY_ROOT@ placeholder: %s\n' "$unit_template" >&2
    exit 1
fi

escape_sed_replacement() {
    printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'
}

render_unit() {
    local escaped_root
    escaped_root=$(escape_sed_replacement "$repository_root")
    sed -e "s|@AUTOWORK_REPOSITORY_ROOT@|$escaped_root|g" "$unit_template"
}

print_plan() {
    local operator
    operator=${USER:-$(id -un)}

    printf '\n# Commands for this setup (sudo is printed, never executed):\n'
    printf '# One-time group setup; both commands are idempotent:\n'
    printf 'getent group %s >/dev/null || sudo groupadd --system %s\n' "$group_name" "$group_name"
    printf 'sudo usermod -aG %s %s\n' "$group_name" "$operator"
    printf '# After joining the group, log out and back in (or run newgrp %s)\n' "$group_name"
    printf 'mkdir -p %s\n' "$unit_dir"
    printf 'install -m 0644 <rendered unit> %s\n' "$unit_target"

    printf '\n# Follow-up commands (not run by this script):\n'
    printf 'systemctl --user daemon-reload\n'
    printf 'systemctl --user enable --now %s\n' "$unit_name"
    printf 'loginctl enable-linger %s   # start at boot without an active login\n' "$operator"
    printf 'systemctl --user status %s\n' "$unit_name"
}

if [[ "$mode" == "apply" ]]; then
    mkdir -p -- "$unit_dir"
    rendered_file=$(mktemp "$unit_dir/.$unit_name.XXXXXX")
    trap 'rm -f -- "$rendered_file"' EXIT
    render_unit >"$rendered_file"
    chmod 0644 "$rendered_file"
    if [[ -f "$unit_target" ]] && cmp -s "$rendered_file" "$unit_target"; then
        printf 'unit unchanged: %s\n' "$unit_target"
        rm -f -- "$rendered_file"
        trap - EXIT
    else
        mv -f -- "$rendered_file" "$unit_target"
        trap - EXIT
        printf 'unit installed: %s\n' "$unit_target"
    fi
else
    printf '# Preview (%s mode); nothing is written.\n' "$mode"
    printf '# Rendered unit: %s\n\n' "$unit_target"
    render_unit
fi

print_plan
