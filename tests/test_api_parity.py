"""The REST API covers every action the UI can take, and this test keeps it so.

UI below maps every page and every form operation in the portal to the
/api/v1/ endpoints that do the same job. The tests fail if

* a URL is added to the site without an entry here (so a new page or form
  can't quietly lack an API),
* a template posts an `op` or `action` that no entry mentions,
* an entry names an endpoint the API doesn't have.
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase
from django.urls import get_resolver

import api.urls  # noqa: F401  (registers every endpoint)
from api.core import ENDPOINTS

# url name -> {operation (or "page" for what the page shows): [API endpoints]}
UI = {
    # accounts
    "accounts:login": {"sign in": ["POST auth/tokens"]},
    "accounts:logout": {"sign out": ["DELETE auth/tokens/<int:pk>"]},
    "accounts:signup": {"sign up": ["POST auth/signup"]},
    "accounts:account": {"page": ["GET me", "GET me/sessions", "GET auth/tokens"], "profile": ["PATCH me"]},
    "accounts:password": {"change password": ["POST me/password"]},
    "accounts:end_session": {"sign one out": ["DELETE me/sessions/<int:pk>"]},
    "accounts:end_other_sessions": {"sign out others": ["POST me/sessions/end-others"]},
    "accounts:tokens": {"create": ["POST auth/tokens"], "revoke": ["DELETE auth/tokens/<int:pk>"]},
    "accounts:admin": {"page": ["GET admin/users", "GET admin/audit"]},
    "accounts:admin_user_update": {"save": ["PATCH admin/users/<int:pk>"]},
    "accounts:admin_audit": {"page": ["GET admin/audit"]},
    # events and the console
    "home": {"page": ["GET events"]},
    "events:organize": {"page": ["GET organize"]},
    "events:create": {"create": ["POST events"]},
    "events:detail": {"page": ["GET events/<slug:slug>"]},
    "events:manage": {"page": ["GET events/<slug:slug>", "GET events/<slug:slug>/submissions",
                               "GET events/<slug:slug>/activity"]},
    "events:manage_details": {"save": ["PATCH events/<slug:slug>"]},
    "events:manage_tracks": {"page": ["GET events/<slug:slug>/tracks"],
                             "save": ["POST events/<slug:slug>/tracks", "PATCH events/<slug:slug>/tracks/<int:pk>"],
                             "delete": ["DELETE events/<slug:slug>/tracks/<int:pk>"]},
    "events:manage_prizes": {"page": ["GET events/<slug:slug>/prizes"],
                             "save": ["POST events/<slug:slug>/prizes", "PATCH events/<slug:slug>/prizes/<int:pk>"],
                             "delete": ["DELETE events/<slug:slug>/prizes/<int:pk>"]},
    "events:manage_questions": {"page": ["GET events/<slug:slug>/questions"],
                                "save": ["POST events/<slug:slug>/questions",
                                         "PATCH events/<slug:slug>/questions/<int:pk>"],
                                "delete": ["DELETE events/<slug:slug>/questions/<int:pk>"]},
    "events:manage_people": {"page": ["GET events/<slug:slug>/organizers"], "add": ["POST events/<slug:slug>/organizers"]},
    "events:manage_people_remove": {"remove": ["DELETE events/<slug:slug>/organizers/<int:pk>"]},
    "events:manage_submissions": {"page": ["GET events/<slug:slug>/submissions"]},
    "events:promote_duplicate": {"promote": ["POST events/<slug:slug>/submissions/<int:pk>/promote"]},
    "events:manage_teams": {"page": ["GET events/<slug:slug>/teams"]},
    "events:manage_activity": {"page": ["GET events/<slug:slug>/activity"]},
    # teams
    "teams:mine": {"page": ["GET events/<slug:slug>/team"], "create": ["POST events/<slug:slug>/team"]},
    "teams:rename": {"rename": ["PATCH events/<slug:slug>/team"]},
    "teams:reset_invite": {"reset": ["POST events/<slug:slug>/team/invite/reset"]},
    "teams:leave": {"leave": ["POST events/<slug:slug>/team/leave"]},
    "teams:remove_member": {"remove": ["DELETE events/<slug:slug>/team/members/<int:user_id>"]},
    "teams:join": {"page": ["GET invites/<str:code>"], "join": ["POST invites/<str:code>/join"]},
    # projects, gallery, comments
    "projects:gallery": {"page": ["GET projects"]},
    "projects:event_gallery": {"page": ["GET projects"]},
    "projects:detail": {"page": ["GET projects/<int:pk>", "GET projects/<int:pk>/comments"]},
    "projects:comment_add": {"post": ["POST projects/<int:pk>/comments"]},
    "projects:comment_remove": {"remove": ["DELETE projects/<int:pk>/comments/<int:comment_id>"]},
    "projects:edit": {"page": ["GET events/<slug:slug>/submission"],
                      "save": ["POST events/<slug:slug>/submission"], "submit": ["POST events/<slug:slug>/submission"],
                      "thumbnail": ["POST events/<slug:slug>/submission/thumbnail"]},
    "projects:withdraw": {"withdraw": ["POST events/<slug:slug>/submission/withdraw"]},
    "projects:image_upload": {"upload": ["POST events/<slug:slug>/submission/images"]},
    "projects:image_delete": {"delete": ["DELETE events/<slug:slug>/submission/images/<int:image_id>"]},
    # judging: the organizer
    "judging:progress": {"page": ["GET events/<slug:slug>/judging/progress"]},
    "judging:rubric": {"page": ["GET events/<slug:slug>/judging/criteria", "GET events/<slug:slug>/judging/config"],
                       "config": ["PATCH events/<slug:slug>/judging/config"],
                       "save": ["POST events/<slug:slug>/judging/criteria",
                                "PATCH events/<slug:slug>/judging/criteria/<int:pk>"],
                       "delete": ["DELETE events/<slug:slug>/judging/criteria/<int:pk>"]},
    "judging:judges": {"page": ["GET events/<slug:slug>/judging/judges", "GET events/<slug:slug>/judging/invites",
                                "GET events/<slug:slug>/judging/conflicts"],
                       "add": ["POST events/<slug:slug>/judging/judges"],
                       "invite": ["POST events/<slug:slug>/judging/invites"],
                       "revoke": ["DELETE events/<slug:slug>/judging/invites/<int:pk>"],
                       "tracks": ["PATCH events/<slug:slug>/judging/judges/<int:pk>"],
                       "remove": ["DELETE events/<slug:slug>/judging/judges/<int:pk>"],
                       "conflict": ["POST events/<slug:slug>/judging/conflicts"]},
    "judging:assignments": {"page": ["GET events/<slug:slug>/judging/assignments",
                                     "GET events/<slug:slug>/judging/batches"],
                            "preview": ["POST events/<slug:slug>/judging/batches"],
                            "commit": ["POST events/<slug:slug>/judging/batches"],
                            "manual": ["POST events/<slug:slug>/judging/assignments"],
                            "remove": ["DELETE events/<slug:slug>/judging/assignments/<int:pk>"]},
    "judging:results": {"page": ["GET events/<slug:slug>/judging/results"],
                        "publish": ["POST events/<slug:slug>/judging/publish"],
                        "unpublish": ["POST events/<slug:slug>/judging/unpublish"]},
    "judging:exports": {"page": ["GET events/<slug:slug>/export/<str:stage>.csv"]},
    "judging:export": {"download": ["GET events/<slug:slug>/export/<str:stage>.csv"]},
    # judging: the judge
    "judging:home": {"page": ["GET judge/events"]},
    "judging:queue": {"page": ["GET judge/events/<slug:slug>/queue"]},
    "judging:score": {"page": ["GET judge/events/<slug:slug>/projects/<int:pk>"],
                      "draft": ["PUT judge/events/<slug:slug>/projects/<int:pk>/score"],
                      "submit": ["PUT judge/events/<slug:slug>/projects/<int:pk>/score"],
                      "submit_next": ["PUT judge/events/<slug:slug>/projects/<int:pk>/score",
                                      "GET judge/events/<slug:slug>/queue"]},
    "judging:conflict": {"conflict": ["POST judge/events/<slug:slug>/projects/<int:pk>/conflict"]},
    "judging:invite": {"accept": ["POST judge-invites/<str:token>/accept"]},
    # community vote
    "voting:ballot": {"page": ["GET events/<slug:slug>/ballot"], "save": ["POST events/<slug:slug>/ballot"],
                      "email": ["POST events/<slug:slug>/ballot/email"],
                      "forget": ["POST events/<slug:slug>/ballot/forget"]},
    "voting:ballot_link": {"page": ["GET events/<slug:slug>/ballot"], "save": ["POST events/<slug:slug>/ballot"]},
    "voting:email_confirm": {"confirm": ["POST events/<slug:slug>/ballot/email/confirm"]},
    "voting:results": {"page": ["GET events/<slug:slug>/vote/results"]},
    "voting:manage": {"page": ["GET events/<slug:slug>/voting", "GET events/<slug:slug>/voting/tally"],
                      "config": ["PATCH events/<slug:slug>/voting"],
                      "publish": ["POST events/<slug:slug>/voting/publish"],
                      "unpublish": ["POST events/<slug:slug>/voting/unpublish"],
                      "new_link": ["POST events/<slug:slug>/voting/link"],
                      "close": ["POST events/<slug:slug>/voting/close"]},
    # anti-abuse
    "integrity:review": {"page": ["GET events/<slug:slug>/integrity"],
                         "exclude": ["POST events/<slug:slug>/integrity/ballots/exclude"],
                         "restore": ["POST events/<slug:slug>/integrity/ballots/<int:pk>/restore"],
                         "hide_duplicate": ["POST events/<slug:slug>/integrity/projects/<int:pk>/hide"],
                         "remove_comments": ["POST events/<slug:slug>/integrity/comments/remove"]},
    # prizes, certificates and records
    "records:manage": {"page": ["GET events/<slug:slug>/awards", "GET events/<slug:slug>/records"],
                       "award": ["POST events/<slug:slug>/awards"],
                       "unaward": ["DELETE events/<slug:slug>/awards/<int:pk>"],
                       "issue": ["POST events/<slug:slug>/records"],
                       "revoke": ["POST records/<str:code>/revoke"]},
    "records:detail": {"page": ["GET records/<str:code>"]},
    "records:pdf": {"download": ["GET records/<str:code>"]},
    "records:json": {"download": ["GET records/<str:code>"]},
    "records:scores": {"page": ["GET records/<str:code>/scores"]},
    "records:verify": {"page": ["GET records/key"], "check": ["POST records/verify"]},
    "records:verify_code": {"page": ["GET records/<str:code>"]},
    # the embeddable gallery
    "embeds:manage": {"page": ["GET events/<slug:slug>/embed"], "save": ["PATCH events/<slug:slug>/embed"]},
    "embeds:widget": {"page": ["GET projects"]},
    "embeds:feed": {"page": ["GET projects"]},
    "judging:public_results": {"page": ["GET events/<slug:slug>/results"]},
    # bulk import and export (the API previews with "preview": true, so there is nothing to cancel)
    "transfer:import": {"preview": ["POST imports"], "commit": ["POST imports"], "cancel": ["POST imports"]},
    "transfer:manage": {"page": ["GET events/<slug:slug>/export/archive.zip"],
                        "preview_teams": ["POST events/<slug:slug>/import/teams"],
                        "preview_judges": ["POST events/<slug:slug>/import/judges"],
                        "apply": ["POST events/<slug:slug>/import/teams", "POST events/<slug:slug>/import/judges"],
                        "cancel": ["POST events/<slug:slug>/import/teams"]},
    "transfer:archive": {"page": ["GET events/<slug:slug>/export/archive.zip"]},
    "transfer:fixture": {"page": ["GET events/<slug:slug>/export/fixtures.json"]},
    "records_key": {"page": ["GET records/key"]},
    # webhooks
    "webhooks:manage": {"page": ["GET events/<slug:slug>/webhooks", "GET webhooks/<int:pk>",
                                 "GET webhooks/<int:pk>/deliveries"],
                        "create": ["POST events/<slug:slug>/webhooks"], "save": ["PATCH webhooks/<int:pk>"],
                        "on": ["PATCH webhooks/<int:pk>"], "off": ["PATCH webhooks/<int:pk>"],
                        "use_receiver": ["PATCH webhooks/<int:pk>"], "delete": ["DELETE webhooks/<int:pk>"],
                        "rotate": ["POST webhooks/<int:pk>/secret"], "test": ["POST webhooks/<int:pk>/test"],
                        "redeliver": ["POST webhooks/<int:pk>/deliveries/<int:delivery_id>/redeliver"]},
    "webhooks:admin": {"page": ["GET admin/webhooks"], "create": ["POST admin/webhooks"]},
}

# Routes that aren't UI actions, and why.
NOT_ACTIONS = {
    "healthz": "a health check for the container",
    "media": "files: scripts fetch the same URLs the API returns",
    "accounts:demo_login": "the demo-mode account switcher; scripts use POST auth/tokens",
    "webhook_receiver": "a machine endpoint itself: the built-in webhook test receiver",
    "api_docs": "the API's own reference",
    "embeds:loader": "a script file for other sites to include, not an action",
    # The older JSON routes are API already, and each lives on under /api/v1/:
    "projects:api_projects": "GET projects", "projects:api_submission": "POST events/<slug:slug>/submission",
    "judging:api_judge_scores": "GET judge/scores", "judging:api_judge_assignments": "GET judge/events/<slug:slug>/queue",
    "judging:api_submit_score": "PUT judge/events/<slug:slug>/projects/<int:pk>/score",
    "judging:api_progress": "GET events/<slug:slug>/judging/progress",
    "judging:api_results": "GET events/<slug:slug>/judging/results",
    "voting:api_ballot": "POST events/<slug:slug>/ballot", "voting:api_results": "GET events/<slug:slug>/vote/results",
}

AVAILABLE = {f"{e.method} {e.path}" for e in ENDPOINTS}


def url_names():
    def walk(patterns, ns=""):
        for p in patterns:
            if hasattr(p, "url_patterns"):
                yield from walk(p.url_patterns, p.namespace or ns)
            elif not str(p.pattern).startswith("api/v1/") or p.name == "api_docs":
                yield f"{ns}:{p.name}" if ns and p.name else p.name
    return set(walk(get_resolver().url_patterns))


class ParityTests(SimpleTestCase):
    def test_every_route_is_mapped_or_explained(self):
        missing = url_names() - set(UI) - set(NOT_ACTIONS)
        self.assertEqual(missing, set(), "these routes have no REST API mapping")

    def test_every_mapped_endpoint_exists(self):
        named = [ep for ops in UI.values() for eps in ops.values() for ep in eps]
        named += [v for v in NOT_ACTIONS.values() if v.split(" ", 1)[0] in {"GET", "POST", "PUT", "PATCH", "DELETE"}]
        self.assertEqual(sorted(set(named) - AVAILABLE), [])

    def test_every_form_operation_in_the_templates_is_mapped(self):
        found = set()
        for path in Path(settings.BASE_DIR, "templates").rglob("*.html"):
            text = path.read_text(encoding="utf-8")
            found |= set(re.findall(r'name="(?:op|action)" value="([a-z_]+)"', text))
        found |= {"on", "off"}  # the webhook switch picks its op in the template
        mapped = {op for ops in UI.values() for op in ops}
        self.assertEqual(sorted(found - mapped), [])

    def test_every_endpoint_says_who_may_call_it_and_what_it_does(self):
        for e in ENDPOINTS:
            self.assertTrue(e.who and e.summary, e.display_path)
