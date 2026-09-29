"""/api/v1/ routes, built from the endpoint table in api/core.py."""

from django.urls import path

from . import core, docs  # noqa: F401  (docs registers the index)
from .endpoints import accounts, embeds, events, judging, records, teams_projects, transfer, voting_integrity, webhooks  # noqa: F401
from . import openapi  # noqa: F401,E402  (last: it refers to schemas the endpoints register)

_paths = []
for _e in core.ENDPOINTS:
    if _e.path not in _paths:
        _paths.append(_e.path)
# Fixed paths before patterns, so records/verify isn't taken for records/<code>.
_paths.sort(key=lambda path: path.count("<"))

urlpatterns = [path("api/v1/docs", docs.reference, name="api_docs")] + [
    path(f"api/v1/{p}", core.dispatcher(p)) for p in _paths
]
