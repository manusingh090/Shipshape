from django.contrib.sessions.models import Session
from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import User, UserSession

from .factories import PASSWORD, make_user


class AuthTests(TestCase):
    def test_signup_logs_you_in(self):
        response = self.client.post(reverse("accounts:signup"), {
            "name": "Wei Chen", "email": "Wei@Example.org",
            "password1": "a long enough passphrase", "password2": "a long enough passphrase",
        })
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(email="wei@example.org")
        self.assertTrue(user.check_password("a long enough passphrase"))
        self.assertEqual(self.client.get(reverse("accounts:account")).status_code, 200)

    def test_weak_or_mismatched_passwords_are_refused(self):
        self.client.post(reverse("accounts:signup"), {
            "name": "Wei", "email": "wei@example.org", "password1": "12345678901", "password2": "12345678901",
        })
        self.assertFalse(User.objects.filter(email="wei@example.org").exists())

    def test_login_and_logout(self):
        make_user("priya@example.org")
        response = self.client.post(reverse("accounts:login"), {"email": "PRIYA@example.org", "password": PASSWORD})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(UserSession.objects.count(), 1)
        self.client.post(reverse("accounts:logout"))
        self.assertEqual(UserSession.objects.count(), 0)
        self.assertEqual(self.client.get(reverse("accounts:account")).status_code, 302)

    def test_logout_needs_post(self):
        self.assertEqual(self.client.get(reverse("accounts:logout")).status_code, 405)

    def test_login_throttle_kicks_in_after_five_failures(self):
        make_user("priya@example.org")
        for _ in range(5):
            self.client.post(reverse("accounts:login"), {"email": "priya@example.org", "password": "wrong"})
        response = self.client.post(reverse("accounts:login"), {"email": "priya@example.org", "password": PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Too many wrong passwords")

    def test_next_parameter_cannot_redirect_off_site(self):
        make_user("priya@example.org")
        response = self.client.post(
            reverse("accounts:login") + "?next=https://evil.example/",
            {"email": "priya@example.org", "password": PASSWORD},
        )
        self.assertEqual(response["Location"], reverse("home"))

    def test_forms_need_a_csrf_token(self):
        make_user("priya@example.org")
        client = Client(enforce_csrf_checks=True)
        response = client.post(reverse("accounts:login"), {"email": "priya@example.org", "password": PASSWORD})
        self.assertEqual(response.status_code, 403)


class SessionTests(TestCase):
    def setUp(self):
        self.priya = make_user("priya@example.org")
        self.laptop, self.phone = Client(), Client()
        for client in (self.laptop, self.phone):
            client.post(reverse("accounts:login"), {"email": "priya@example.org", "password": PASSWORD})

    def test_you_can_end_your_other_sessions(self):
        self.assertEqual(UserSession.objects.filter(user=self.priya).count(), 2)
        self.laptop.post(reverse("accounts:end_other_sessions"))
        self.assertEqual(self.phone.get(reverse("accounts:account")).status_code, 302)
        self.assertEqual(self.laptop.get(reverse("accounts:account")).status_code, 200)

    def test_changing_password_signs_out_other_devices(self):
        self.laptop.post(reverse("accounts:password"), {
            "old_password": PASSWORD, "new_password1": "a brand new passphrase", "new_password2": "a brand new passphrase",
        })
        self.assertEqual(self.laptop.get(reverse("accounts:account")).status_code, 200)
        self.assertEqual(self.phone.get(reverse("accounts:account")).status_code, 302)
        self.assertEqual(UserSession.objects.filter(user=self.priya).count(), 1)

    def test_deactivated_users_lose_their_sessions(self):
        self.priya.is_active = False
        self.priya.save()
        self.assertEqual(self.laptop.get(reverse("accounts:account")).status_code, 302)

    def test_cookie_is_named_session(self):
        self.assertIn("session", self.laptop.cookies)
        self.assertTrue(Session.objects.filter(session_key=self.laptop.cookies["session"].value).exists())
