"""Agentic operator adapters (M24 lot B): typed clients for host sessions.

Nothing here launches a process or names a CLI flag.  The host workspace agent
owns the supervisor and the provider profiles; the backend only speaks the
``/v1/sessions/**`` wire through :class:`HttpHostSessionsClient`.
"""
