# Request to orchestrator — archive_list still reports 7z unavailable

Investigation `782ff20c-8bfb-4ac0-a1a8-356e8b7bbe03`; operator B.

Fresh `local.run_command` invocation `b013-archive-list-retry` / canonical invocation `c2d98e17-2d69-4465-a127-aff03bb83f52`, state v114, completed with invocation/output status `succeeded` and exit code 0. Its bounded `archive_list` output reports `archive_status=7z_unavailable`, zero total/reported members, zero bytes read, and no truncation. It used the exact archive association previously targeted by A and `max_members=200`.

Please reconcile why the retried archive worker still reports `7z_unavailable` after the redeployment note, or expose the corrected archive-list capability. The operator is continuing with canonical `zip_members` evidence and the independent targeted extraction path. No CTI hash, member name or sample bytes are included here.
