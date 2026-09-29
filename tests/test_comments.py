"""Comments on gallery projects, and what teams may see of judging."""

import csv
import io
from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import User
from events.models import Activity, EventRole, log_activity
from projects.models import Comment, Project

from .factories import add_staff, make_event, make_team, make_user


class CommentSetup(TestCase):
    def setUp(self):
        self.event = make_event(slug="talk-hack", phase="closed")
        self.organizer = make_user("org@example.org", role=User.Role.ORGANIZER)
        add_staff(self.event, self.organizer)
        self.captain = make_user("cap@example.org", name="Cara Captain")
        self.team = make_team(self.event, self.captain, name="Night Owls")
        self.project = Project.objects.create(
            event=self.event, team=self.team, track=self.event.tracks.get(), title="Quiet Hours", tagline="x",
            status=Project.Status.SUBMITTED, submitted_at=self.event.submissions_close_at - timedelta(hours=1))
        self.reader = make_user("reader@example.org", name="Rhea Reader")
        self.judge = make_user("judge@example.org", name="Jules Judge")
        EventRole.objects.create(event=self.event, user=self.judge, role=EventRole.Role.JUDGE)

    def client_for(self, user=None):
        client = Client()
        if user:
            client.force_login(user)
        return client

    def post(self, user, body):
        return self.client_for(user).post(reverse("projects:comment_add", args=[self.project.pk]), {"body": body})


class CommentTests(CommentSetup):
    def test_anyone_reads_signed_in_people_write(self):
        self.assertRedirects(self.post(self.reader, "Lovely idea. **More** please."),
                             self.project.get_absolute_url() + "#comments", fetch_redirect_response=False)
        page = Client().get(self.project.get_absolute_url())
        self.assertContains(page, "Rhea Reader")
        self.assertContains(page, "<strong>More</strong>", html=True)
        self.assertContains(page, "Sign in</a> to comment")
        self.assertNotContains(page, 'name="body"')
        self.assertEqual(Client().post(reverse("projects:comment_add", args=[self.project.pk]),
                                       {"body": "anonymous"}).status_code, 302)  # to the sign-in page
        self.assertEqual(Comment.objects.count(), 1)

    def test_comment_text_cannot_inject_markup(self):
        self.post(self.reader, '<script>alert(1)</script> [x](javascript:alert(1))')
        page = Client().get(self.project.get_absolute_url())
        self.assertNotContains(page, "<script>alert(1)</script>")
        self.assertNotContains(page, 'href="javascript:')

    def test_judges_of_the_event_cannot_comment(self):
        self.post(self.judge, "Solid work, I'd give it a 5.")
        self.assertFalse(Comment.objects.exists())
        page = self.client_for(self.judge).get(self.project.get_absolute_url())
        self.assertContains(page, "judging this event")
        self.assertNotContains(page, 'name="body"')

    def test_team_members_and_organizers_are_labelled(self):
        self.post(self.captain, "Thanks for looking!")
        self.post(self.organizer, "Demo at 3pm in room B.")
        page = Client().get(self.project.get_absolute_url())
        self.assertContains(page, ">Team<")
        self.assertContains(page, ">Organizer<")

    def test_drafts_and_duplicates_have_no_thread(self):
        draft = Project.objects.create(event=self.event, team=make_team(self.event, make_user("d@example.org"),
                                                                         name="Drafty"),
                                       title="Half done", status=Project.Status.DRAFT)
        response = self.client_for(self.reader).post(reverse("projects:comment_add", args=[draft.pk]), {"body": "hi"})
        self.assertEqual(response.status_code, 404)
        dupe = Project.objects.create(event=self.event, team=self.team, title="Quiet Hours again",
                                      status=Project.Status.SUBMITTED, duplicate_of=self.project)
        self.client_for(self.organizer).post(reverse("projects:comment_add", args=[dupe.pk]), {"body": "hi"})
        self.assertFalse(Comment.objects.exists())

    def test_organizers_can_switch_comments_off(self):
        self.post(self.reader, "First!")
        self.event.comments_enabled = False
        self.event.save()
        self.post(self.reader, "Second!")
        self.assertEqual(Comment.objects.count(), 1)
        page = Client().get(self.project.get_absolute_url())
        self.assertContains(page, "First!")  # what's there stays readable
        self.assertContains(page, "turned comments off")

    def test_length_empty_repeats_and_rate(self):
        self.post(self.reader, "   ")
        self.post(self.reader, "x" * 2001)
        self.assertFalse(Comment.objects.exists())
        self.post(self.reader, "Same thing")
        self.post(self.reader, "Same thing")
        self.assertEqual(Comment.objects.count(), 1)
        for n in range(6):
            self.post(self.reader, f"comment {n}")
        self.assertEqual(Comment.objects.count(), 5)  # five a minute

    def test_authors_remove_their_own_organizers_remove_anyones_with_a_reason(self):
        self.post(self.reader, "Oops, wrong project")
        mine = Comment.objects.get()
        url = reverse("projects:comment_remove", args=[self.project.pk, mine.pk])
        self.assertEqual(self.client_for(self.captain).post(url).status_code, 403)
        self.client_for(self.reader).post(url)
        mine.refresh_from_db()
        self.assertIsNotNone(mine.removed_at)
        self.assertNotContains(Client().get(self.project.get_absolute_url()), "Oops, wrong project")

        self.post(self.reader, "You are all idiots")
        rude = Comment.objects.latest("created_at")
        url = reverse("projects:comment_remove", args=[self.project.pk, rude.pk])
        self.client_for(self.organizer).post(url, {"reason": ""})
        rude.refresh_from_db()
        self.assertIsNone(rude.removed_at)  # a reason is required
        self.client_for(self.organizer).post(url, {"reason": "personal attack"})
        rude.refresh_from_db()
        self.assertEqual(rude.removal_reason, "personal attack")
        public = Client().get(self.project.get_absolute_url())
        self.assertNotContains(public, "You are all idiots")
        self.assertContains(public, "removed by the organizers")
        self.assertContains(self.client_for(self.organizer).get(self.project.get_absolute_url()), "personal attack")
        self.assertTrue(Activity.objects.filter(verb="comment.removed").exists())

    def test_you_can_remove_what_you_posted_straight_from_the_thread(self):
        response = self.client_for(self.reader).post(reverse("projects:comment_add", args=[self.project.pk]),
                                                     {"body": "Posted by mistake"}, follow=True)
        self.assertContains(response, "Remove my comment")
        comment = Comment.objects.get()
        remove_url = reverse("projects:comment_remove", args=[self.project.pk, comment.pk])
        self.assertContains(response, f'action="{remove_url}"')
        for other in (None, self.captain):  # nobody else gets the button on your comment
            self.assertNotContains(self.client_for(other).get(self.project.get_absolute_url()), "Remove my comment")
        response = self.client_for(self.reader).post(remove_url, follow=True)
        self.assertContains(response, "Your comment was removed.")
        self.assertNotContains(response, "Posted by mistake")
        # Gone for organizers too; only the export keeps it.
        self.assertNotContains(self.client_for(self.organizer).get(self.project.get_absolute_url()), "Posted by mistake")
        export = self.client_for(self.organizer).get(reverse("judging:export", args=[self.event.slug, "comments"]))
        self.assertContains(export, "Posted by mistake")

    def test_comments_export_is_organizer_only(self):
        self.post(self.reader, "Nice")
        url = reverse("judging:export", args=[self.event.slug, "comments"])
        self.assertEqual(self.client_for(self.reader).get(url).status_code, 403)
        rows = list(csv.DictReader(io.StringIO(self.client_for(self.organizer).get(url).content.decode())))
        self.assertEqual([(r["author_email"], r["body"]) for r in rows], [("reader@example.org", "Nice")])


class TeamHistoryTests(CommentSetup):
    """A team's own history must not show judging: who reviews them, when a
    judge scored, or a judge's conflict and its reason."""

    def test_judging_entries_stay_out_of_the_team_pages(self):
        for verb, detail in [("score.submitted", "Jules Judge submitted a score"),
                             ("judge.conflict", "Jules Judge with Quiet Hours: I MENTORED THEM"),
                             ("judging.assigned", "assigned Quiet Hours to Jules Judge"),
                             ("voting.published", "published the community vote results")]:
            log_activity(self.event, self.judge, verb, detail, team=self.team, project=self.project)
        log_activity(self.event, self.captain, "project.submitted", "SUBMITTED THE PROJECT", team=self.team)
        client = self.client_for(self.captain)
        for url in (reverse("teams:mine", args=[self.event.slug]), reverse("projects:edit", args=[self.event.slug])):
            page = client.get(url)
            self.assertContains(page, "SUBMITTED THE PROJECT")
            for secret in ("Jules Judge", "I MENTORED THEM", "published the community vote"):
                self.assertNotContains(page, secret)
        organizer_log = self.client_for(self.organizer).get(reverse("events:manage_activity", args=[self.event.slug]))
        self.assertContains(organizer_log, "I MENTORED THEM")
