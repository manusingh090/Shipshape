"""Write the OpenAPI document to src/api/openapi.json (or check it's current)."""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

TARGET = Path(__file__).resolve().parents[2] / "openapi.json"


class Command(BaseCommand):
    help = "Write the REST API's OpenAPI 3.1 document to src/api/openapi.json. --check only compares."

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true", help="Fail if the file is out of date; write nothing.")

    def handle(self, *args, check=False, **options):
        import api.urls  # noqa: F401  (registers every endpoint)
        from api.openapi import document

        text = json.dumps(document(), indent=2, ensure_ascii=False) + "\n"
        if check:
            if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != text:
                raise CommandError(f"{TARGET} is out of date. Run: python src/manage.py openapi")
            self.stdout.write("openapi.json is current.")
            return
        TARGET.write_text(text, encoding="utf-8", newline="\n")
        doc = json.loads(text)
        count = sum(len(item) for item in doc["paths"].values())
        self.stdout.write(f"Wrote {TARGET} ({count} operations, {len(text) // 1024} KB).")
