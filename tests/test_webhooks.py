"""Webhooks: what's sent, how it's signed, retried, refused and received."""

import json
import socket
import time
from datetime import timedelta
from unittest import mock

from django.db import transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from events.models import Activity, log_activity
from projects.models import Project
from voting.models import VotingConfig
from webhooks import delivery as sender
from webhooks.models import Delivery, Webhook

from .factories import add_staff, make_event, make_team, make_user
from .test_api import ApiClient


def public_dns(host, port, *args, **kwargs):
    """Pretend every host is a public address, so tests never touch DNS."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


class Setup(TestCase):
    def setUp(self):
        self.event = make_event(slug="hooked", phase="open")
        self.organizer = make_user("org@example.org", role=User.Role.ORGANIZER)
        add_staff(self.event, self.organizer)
        self.hook = Webhook.objects.create(event=self.event, url="https://hooks.example.net/in")

    def log(self, verb="project.submitted", detail="submitted “Quiet Hours”", event=None):
        with self.captureOnCommitCallbacks(execute=True):
            log_activity(event if event is not None else self.event, self.organizer, verb, detail)


@mock.patch("webhooks.delivery.socket.getaddrinfo", public_dns)
class QueueTests(Setup):
    def test_every_logged_action_is_queued_for_the_events_webhooks(self):
        self.log()
        d = Delivery.objects.get()
        payload = json.loads(d.payload)
        self.assertEqual((d.kind, payload["event"]["slug"], payload["detail"]),
                         ("project.submitted", "hooked", "submitted “Quiet Hours”"))
        other = make_event(slug="elsewhere", phase="open")
        self.log(event=other)
        self.assertEqual(Delivery.objects.count(), 1)  # another event's webhooks only

    def test_categories_filter_what_is_sent(self):
        self.hook.categories = "refused,judging"
        self.hook.save()
        self.log("project.submitted")
        self.log("late.refused", "tried to edit after the deadline")
        self.log("score.submitted", "submitted a score")
        self.assertEqual(sorted(Delivery.objects.values_list("kind", flat=True)), ["late.refused", "score.submitted"])

    def test_nothing_is_announced_for_an_action_that_was_rolled_back(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    log_activity(self.event, self.organizer, "project.submitted", "never happened")
                    raise RuntimeError
            except RuntimeError:
                pass
        self.assertFalse(Delivery.objects.exists())

    def test_platform_webhooks_hear_the_platform_log(self):
        platform = Webhook.objects.create(event=None, url="https://ops.example.net/in")
        with self.captureOnCommitCallbacks(execute=True):
            log_activity(None, None, "login.locked", "sign-in for x@example.org locked")
        self.assertEqual(list(Delivery.objects.values_list("webhook", flat=True)), [platform.pk])

    def test_ballots_are_never_announced(self):
        VotingConfig.objects.create(event=self.event, is_enabled=True, access="account",
                                    opens_at=timezone.now() - timedelta(hours=1),
                                    closes_at=timezone.now() + timedelta(days=1))
        from events.models import Event
        Event.objects.filter(pk=self.event.pk).update(submissions_close_at=timezone.now() - timedelta(hours=2),
                                                       starts_at=timezone.now() - timedelta(days=2))
        team = make_team(self.event, make_user("t@example.org"))
        p = Project.objects.create(event=self.event, team=team, title="P", status="submitted",
                                   submitted_at=timezone.now() - timedelta(hours=3))
        with self.captureOnCommitCallbacks(execute=True):
            r = ApiClient(make_user("voter@example.org")).post("events/hooked/ballot", {"votes": {str(p.pk): 2}})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(Delivery.objects.exists())


class SigningTests(TestCase):
    def test_a_signature_verifies_and_tampering_or_replay_doesnt(self):
        body = b'{"type":"ping"}'
        now = int(time.time())
        header = sender.signature("whsec_abc", now, body)
        self.assertTrue(sender.verify("whsec_abc", header, body))
        self.assertFalse(sender.verify("whsec_abc", header, body + b" "))          # body changed
        self.assertFalse(sender.verify("whsec_other", header, body))                # wrong secret
        self.assertFalse(sender.verify("whsec_abc", sender.signature("whsec_abc", now - 600, body), body))  # too old
        self.assertFalse(sender.verify("whsec_abc", "t=nonsense", body))


@mock.patch("webhooks.delivery.socket.getaddrinfo", public_dns)
class SendingTests(Setup):
    def queued(self):
        self.log()
        return Delivery.objects.get()

    def test_a_2xx_delivers_and_the_request_is_signed(self):
        d = self.queued()
        with mock.patch("webhooks.delivery._post", return_value=(200, "ok")) as post:
            self.assertEqual(sender.deliver_due(), 1)
        url, body, headers = post.call_args.args
        self.assertEqual(url, "https://hooks.example.net/in")
        self.assertTrue(sender.verify(self.hook.secret, headers["Shipshape-Signature"], body))
        self.assertEqual(json.loads(body)["id"], str(d.uid))
        self.assertEqual(headers["Shipshape-Event"], "project.submitted")
        d.refresh_from_db()
        self.assertEqual((d.status, d.last_status), ("succeeded", 200))
        with mock.patch("webhooks.delivery._post", return_value=(200, "ok")) as post:
            self.assertEqual(sender.deliver_due(), 0)  # never sent twice

    def test_failures_back_off_then_give_up(self):
        d = self.queued()
        with mock.patch("webhooks.delivery._post", return_value=(500, "boom")):
            sender.deliver_due()
            d.refresh_from_db()
            self.assertEqual((d.status, d.attempts), ("pending", 1))
            self.assertGreater(d.next_attempt_at, timezone.now() + timedelta(seconds=50))
            self.assertEqual(sender.deliver_due(), 0)  # not due yet
            for _ in range(sender.MAX_ATTEMPTS - 1):
                Delivery.objects.filter(pk=d.pk).update(next_attempt_at=timezone.now())
                sender.deliver_due()
        d.refresh_from_db()
        self.assertEqual((d.status, d.attempts), ("failed", sender.MAX_ATTEMPTS))

    def test_a_webhook_that_keeps_failing_switches_itself_off(self):
        with mock.patch("webhooks.delivery._post", side_effect=OSError("connection refused")):
            for _ in range(sender.DISABLE_AFTER):
                self.log()
                d = Delivery.objects.filter(status="pending").get()
                d.attempts = sender.MAX_ATTEMPTS - 1
                d.save()
                sender.deliver_due()
        self.hook.refresh_from_db()
        self.assertFalse(self.hook.is_active)
        self.assertIn("in a row", self.hook.disabled_reason)
        self.assertTrue(Activity.objects.filter(verb="webhook.disabled").exists())
        self.log()
        with mock.patch("webhooks.delivery._post") as post:
            sender.deliver_due()
        post.assert_not_called()  # switched-off webhooks queue nothing and send nothing


class DestinationTests(Setup):
    def resolve_to(self, address):
        return mock.patch("webhooks.delivery.socket.getaddrinfo",
                          lambda h, p, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, p))])

    @override_settings(WEBHOOK_ALLOW_LOCAL=False)
    def test_the_server_itself_and_cloud_metadata_are_off_limits(self):
        for address in ("127.0.0.1", "169.254.169.254", "0.0.0.0"):
            with self.resolve_to(address), mock.patch("webhooks.delivery._post") as post:
                d = sender.ping(self.hook)
            post.assert_not_called()
            self.assertEqual(d.status, "failed")
            self.assertIn("may not be sent to", d.last_error)

    @override_settings(WEBHOOK_ALLOW_LOCAL=False)
    def test_a_lan_receiver_is_fine(self):
        with self.resolve_to("192.168.1.20"), mock.patch("webhooks.delivery._post", return_value=(204, "")):
            self.assertEqual(sender.ping(self.hook).status, "succeeded")

    @override_settings(WEBHOOK_ALLOW_LOCAL=True)
    def test_demo_mode_may_use_the_built_in_receiver_end_to_end(self):
        """The real signing path, delivered through Django's test client to the
        portal's own receiver, which checks the signature like a receiver should."""
        self.hook.url = "http://127.0.0.1:8080" + reverse("webhook_receiver", args=[self.hook.pk])
        self.hook.save()
        client = Client()

        def via_client(url, body, headers):
            extra = {"HTTP_" + k.upper().replace("-", "_"): v for k, v in headers.items() if k != "Content-Type"}
            r = client.post(reverse("webhook_receiver", args=[self.hook.pk]), body, content_type="application/json", **extra)
            return r.status_code, r.content.decode()

        with self.resolve_to("127.0.0.1"), mock.patch("webhooks.delivery._post", side_effect=via_client):
            d = sender.ping(self.hook)
        self.assertEqual(d.status, "succeeded")
        self.assertIn('"verified": true', d.response_excerpt)
        forged = client.post(reverse("webhook_receiver", args=[self.hook.pk]), b'{"type":"ping"}',
                             content_type="application/json", HTTP_SHIPSHAPE_SIGNATURE="t=1,v1=00")
        self.assertEqual(forged.status_code, 400)


@mock.patch("webhooks.delivery.socket.getaddrinfo", public_dns)
class ManageTests(Setup):
    def test_the_console_page_adds_tests_and_logs(self):
        browser = Client()
        browser.force_login(self.organizer)
        url = reverse("webhooks:manage", args=[self.event.slug])
        browser.post(url, {"op": "create", "url": "https://chat.example.net/hook", "categories": ["submissions"]})
        hook = Webhook.objects.get(url="https://chat.example.net/hook")
        self.assertEqual(hook.category_list, ["submissions"])
        with mock.patch("webhooks.delivery._post", return_value=(200, "thanks")):
            page = browser.post(url, {"op": "test", "id": hook.pk}, follow=True)
        self.assertContains(page, "Test delivered: 200")
        self.assertTrue(Activity.objects.filter(verb="webhook.created").exists())
        outsider = Client()
        outsider.force_login(make_user("x@example.org"))
        self.assertEqual(outsider.get(url).status_code, 403)

    def test_the_api_manages_webhooks_and_keeps_them_to_their_owners(self):
        org = ApiClient(self.organizer)
        made = org.post("events/hooked/webhooks", {"url": "https://a.example.net/x", "categories": ["refused"]})
        self.assertEqual(made.status_code, 200, made.content)
        self.assertTrue(made.json()["secret"].startswith("whsec_"))
        pk = made.json()["id"]
        old = made.json()["secret"]
        self.assertNotEqual(org.post(f"webhooks/{pk}/secret").json()["secret"], old)
        self.assertFalse(org.patch(f"webhooks/{pk}", {"is_active": False}).json()["is_active"])
        with mock.patch("webhooks.delivery._post", return_value=(200, "")):
            ping = org.post(f"webhooks/{pk}/test").json()
            self.assertEqual(ping["status"], "succeeded")
            again = org.post(f"webhooks/{pk}/deliveries/{ping['id']}/redeliver").json()
        self.assertNotEqual(again["uid"], ping["uid"])
        self.assertEqual(org.get(f"webhooks/{pk}/deliveries").json()["count"], 2)
        stranger = ApiClient(make_user("s@example.org"))
        self.assertEqual(stranger.get(f"webhooks/{pk}").status_code, 404)
        self.assertEqual(stranger.post("admin/webhooks", {"url": "https://x.example.net"}).status_code, 403)
        self.assertEqual(org.delete(f"webhooks/{pk}").status_code, 200)
