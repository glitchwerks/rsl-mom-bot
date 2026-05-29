"""mom_bot.migrations — helpers for the Container Apps Job migration entrypoint.

This sub-package is imported by ``migrate.sh`` (via ``python -m``) to
perform token acquisition and other migration-time tasks that require Python
but would otherwise depend on external CLI tools (e.g. ``curl``) not
present in the ``python:3.12-slim`` base image.
"""
