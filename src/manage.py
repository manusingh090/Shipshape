#!/usr/bin/env python
import os
import sys
from pathlib import Path


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "portal.settings")
    # The test suite lives beside src/, not inside it. Make that package
    # importable so dotted labels like tests.test_judging work.
    root = Path(__file__).resolve().parent.parent
    if (root / "tests" / "__init__.py").exists():
        sys.path.append(str(root))
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
