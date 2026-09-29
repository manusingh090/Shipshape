"""Publishing the judges' results, the results page, and closing a vote early."""

from datetime import timedelta

from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from events.deadline import JudgingClosed
from events.models import Award, Event, Prize
from judging import publishing
from judging.errors import JudgingError
from judging.models import JudgingConfig, Score
from judging.scoring import save_score
from projects.models import Project
from voting import services as vote
from voting.models import VotingConfig

from .factories import make_event, make_user
from .test_openapi import SLUG, Journey


class PublishingTests(Journey):
    def page(self, user=None):
        client = Client()
        if user:
            client.force_login(user)
        return client.get(reverse("judging:public_results", args=[SLUG]))

    def publish(self, **kw):
        return publishing.publish(self.organizer, Event.objects.get(pk=self.event.pk), **kw)

    def priya_project(self):
        return Project.objects.get(team__memberships__user=self.priya, duplicate_of__isnull=True)

    def test_nobody_but_organizers_sees_the_ranking_before_it_is_published(self):
        top = publishing.ranking(self.event)[0].project
        for user in (None, self.priya, self.judge):
            page = self.page(user)
            self.assertEqual(page.status_code, 200)
            self.assertContains(page, "aren't published yet")
            self.assertNotContains(page, "results-table")
        preview = self.page(self.organizer)
        self.assertContains(preview, "Organizer preview")
        self.assertContains(preview, top.title)

    def test_publishing_shows_everyone_and_closes_judging(self):
        self.publish()
        event = Event.objects.get(pk=self.event.pk)
        self.assertLessEqual(event.judging_ends_at, timezone.now())
        self.assertEqual(publishing.state(event), "published")
        top = publishing.ranking(event)[0].project
        self.assertContains(self.page(), top.title)
        # Final means final: no judge can change a score now.
        score = Score.objects.filter(event=event, judge=self.judge, submitted_at__isnull=False).first()
        marks = {i.criterion_id: i.value for i in score.items.all()}
        with self.assertRaises(JudgingClosed):
            save_score(self.judge, event, score.project_id, marks, submit=True)
        self.assertTrue(event.activity.filter(verb="judging.published").exists())

    def test_a_team_sees_its_place_and_only_its_own_feedback_when_shared(self):
        mine = self.priya_project()
        self.publish()
        page = self.page(self.priya)
        self.assertContains(page, f"Your project: {mine.title}")
        self.assertNotContains(page, "What the judges said")  # not shared
        JudgingConfig.objects.filter(event=self.event).update(share_feedback=True)
        page = self.page(self.priya)
        self.assertContains(page, "What the judges said")
        comments = list(Score.objects.filter(project=mine, submitted_at__isnull=False).exclude(comment="")
                        .values_list("comment", flat=True))
        for comment in comments:
            self.assertContains(page, comment)
        for judge in User.objects.filter(judging_scores__project=mine).distinct():
            self.assertNotContains(page, judge.display_name)  # never who wrote it
        others = Score.objects.filter(event=self.event).exclude(project=mine).exclude(comment="").exclude(
            comment__in=comments).values_list("comment", flat=True)
        text = page.content.decode()
        self.assertFalse(any(c in text for c in set(others) if len(c) > 12))

    def test_publishing_needs_the_deadline_and_something_to_rank(self):
        early = make_event(slug="still-open")
        org = make_user("org2@example.org", role=User.Role.ORGANIZER)
        with self.assertRaises(JudgingError):
            publishing.publish(org, early)
        empty = make_event(slug="no-scores", phase="closed")
        with self.assertRaises(JudgingError):
            publishing.publish(org, empty)

    def test_reopening_judging_takes_the_results_down(self):
        self.publish()
        from events import services
        from events.forms import EventForm
        event = Event.objects.get(pk=self.event.pk)
        data = {f: getattr(event, f) for f in ("name", "slug", "timezone", "max_team_size", "is_published",
                                                "comments_enabled", "gallery_before_deadline")}
        tz = event.tzinfo
        for f in ("starts_at", "submissions_close_at"):
            data[f] = getattr(event, f).astimezone(tz).strftime("%Y-%m-%dT%H:%M")
        data["judging_ends_at"] = (timezone.now() + timedelta(days=2)).astimezone(tz).strftime("%Y-%m-%dT%H:%M")
        form = EventForm(data, instance=event)
        self.assertTrue(form.is_valid(), form.errors)
        services.update_event(self.organizer, event, form)
        self.assertEqual(publishing.state(Event.objects.get(pk=event.pk)), "unpublished")
        self.assertNotContains(self.page(), "results-table")

    def test_winners_can_be_announced_with_the_results(self):
        later = timezone.now() + timedelta(days=5)
        Event.objects.filter(pk=self.event.pk).update(results_at=later)
        prize = Prize.objects.create(event=self.event, name="Grand prize")
        Award.objects.create(prize=prize, project=self.listed[0], awarded_by=self.organizer)
        self.assertContains(self.page(), "Winners are announced")
        self.publish(announce_winners=True)
        self.assertLessEqual(Event.objects.get(pk=self.event.pk).results_at, timezone.now())
        self.assertContains(self.page(), "Grand prize")

    def test_the_console_publishes_and_takes_down(self):
        client = Client()
        client.force_login(self.organizer)
        url = reverse("judging:results", args=[SLUG])
        self.assertContains(client.get(url), "Publish the results")
        client.post(url, {"op": "publish", "share_feedback": "on"})
        self.assertTrue(JudgingConfig.objects.get(event=self.event).share_feedback)
        self.assertContains(client.get(url), "Take them down")
        client.post(url, {"op": "unpublish"})
        self.assertEqual(publishing.state(Event.objects.get(pk=self.event.pk)), "unpublished")
        outsider = Client()
        outsider.force_login(self.priya)
        outsider.post(url, {"op": "publish"})
        self.assertEqual(publishing.state(Event.objects.get(pk=self.event.pk)), "unpublished")

    def test_the_api_does_the_same(self):
        org, me = self.api(self.organizer), self.api(self.priya)
        hidden = self.call(me, "GET", f"events/{SLUG}/results")
        self.assertIsNone(hidden["ranking"])
        self.assertEqual(hidden["state"], "unpublished")
        self.call(org, "POST", f"events/{SLUG}/judging/publish", {"share_feedback": True})
        shown = self.call(me, "GET", f"events/{SLUG}/results")
        self.assertTrue(shown["ranking"])
        self.assertEqual(shown["yours"]["project"], self.priya_project().pk)
        self.assertIsNotNone(shown["yours"]["feedback"])
        self.call(org, "POST", f"events/{SLUG}/judging/unpublish")
        self.call(me, "POST", f"events/{SLUG}/judging/publish", {}, status=403)

    def test_the_event_page_links_the_results(self):
        page = Client().get(self.event.get_absolute_url())
        self.assertContains(page, reverse("judging:public_results", args=[SLUG]))


class CloseVotingTests(Journey):
    def setUp(self):
        self.config = VotingConfig.objects.create(event=self.event, is_enabled=True, access="account",
                                                  closes_at=timezone.now() + timedelta(days=10))

    def test_an_organizer_can_close_voting_early_and_then_publish(self):
        client = Client()
        client.force_login(self.organizer)
        url = reverse("voting:manage", args=[SLUG])
        self.assertContains(client.get(url), "Close voting now")
        client.post(url, {"op": "close"})
        self.assertEqual(vote.results_state(self.event, VotingConfig.objects.get(pk=self.config.pk)), "awaiting_review")
        self.assertContains(client.get(url), "Publish the results")
        client.post(url, {"op": "publish"})
        self.assertContains(Client().get(reverse("judging:public_results", args=[SLUG])), "The community vote")

    def test_closing_twice_or_as_a_participant_is_refused(self):
        self.call(self.api(self.priya), "POST", f"events/{SLUG}/voting/close", {}, status=403)
        self.call(self.api(self.organizer), "POST", f"events/{SLUG}/voting/close", {})
        self.call(self.api(self.organizer), "POST", f"events/{SLUG}/voting/close", {}, status=400)
        self.assertTrue(self.event.activity.filter(verb="voting.closed").exists())
