"""Host session supervisor for M24-05 (H1): state, processes, logs, fake CLI.

The package is stdlib-only and lives in the workspace agent process. It serves
``/v1/sessions/**`` on the same AF_UNIX socket; it launches only closed
``SessionProfile`` registrations and keeps every byte of runtime state, logs
and the one-shot secret handoff socket under
``<runtime>/sessions/<session_id>/``, never inside a workspace.

Normative source: ``docs/design/investigation-workshop/SESSION-RUNNER.md``.
"""
