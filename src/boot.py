"""Container entrypoint: migrate, seed, then hand the process over to gunicorn.

Written in Python rather than shell so a Windows checkout with CRLF line
endings can't break the container.
"""

import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "portal.settings")
    port = os.environ.get("PORT", "8080")
    # How the portal reaches itself from inside the container (the built-in
    # webhook test receiver). Settings read it at start-up, so set it first.
    os.environ.setdefault("SELF_URL", f"http://127.0.0.1:{port}")
    import django
    from django.core.management import call_command

    django.setup()
    call_command("migrate", interactive=False, verbosity=1)
    call_command("seed")

    workers = os.environ.get("WEB_CONCURRENCY", "3")
    # Each gunicorn worker sends webhook deliveries in a background thread.
    # Set only now, so migrating and seeding never start one.
    os.environ.setdefault("WEBHOOK_WORKER", "1")
    args = [
        "gunicorn", "portal.wsgi:application",
        "--bind", f"0.0.0.0:{port}",
        "--workers", workers,
        "--timeout", "60",
        "--access-logfile", "-",
        "--error-logfile", "-",
        # gunicorn 26 opens a control socket in the working directory by
        # default; the app directory is read-only for the runtime user.
        "--no-control-socket",
    ]
    sys.stdout.flush()
    os.execvp("gunicorn", args)


if __name__ == "__main__":
    main()
