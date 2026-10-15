"""Regression coverage for the allauth adapter overrides in glitchtip.adapters.

CustomSocialAccountAdapter.open_http_session builds the aiohttp session used
for every outbound social-auth request (OAuth token exchange, userinfo). It
merges the project-wide session config (User-Agent, proxy trust, max field
size) with allauth's own per-call REQUESTS_TIMEOUT. The project-wide config
already carries a `timeout`, so the two timeout sources must be merged, not
passed as separate kwargs -- otherwise ClientSession() raises TypeError ("got
multiple values for keyword argument 'timeout'") and every Google login
callback 500s.
"""

import asyncio
import logging
from smtplib import SMTPException

import aiohttp
from allauth.account.models import EmailAddress
from allauth.socialaccount import app_settings as socialaccount_app_settings
from django.core.mail.backends.base import BaseEmailBackend
from django.test import SimpleTestCase, TestCase, override_settings

from glitchtip.adapters import CustomSocialAccountAdapter


class OpenHttpSessionTestCase(SimpleTestCase):
    def _open_and_close(self):
        adapter = CustomSocialAccountAdapter()

        async def run():
            # ClientSession must be created inside a running loop.
            session = adapter.open_http_session()
            try:
                return session.timeout.total, dict(session.headers)
            finally:
                await session.close()

        return asyncio.run(run())

    def test_open_http_session_does_not_collide_on_timeout(self):
        # The bug was a TypeError here; simply reaching the assertions proves
        # the duplicate-kwarg collision is gone.
        total, _ = self._open_and_close()
        self.assertEqual(total, socialaccount_app_settings.REQUESTS_TIMEOUT)

    def test_open_http_session_keeps_project_config(self):
        # The project-wide non-timeout entries (e.g. the User-Agent) still apply.
        _, headers = self._open_and_close()
        self.assertTrue(headers.get("User-Agent", "").startswith("GlitchTip/"))

    def test_open_http_session_is_a_client_session(self):
        adapter = CustomSocialAccountAdapter()

        async def run():
            session = adapter.open_http_session()
            try:
                self.assertIsInstance(session, aiohttp.ClientSession)
            finally:
                await session.close()

        asyncio.run(run())


class FailingEmailBackend(BaseEmailBackend):
    """A transport that refuses every message, like a provider rejecting the
    sender or an unreachable SMTP host."""

    def send_messages(self, email_messages):
        raise SMTPException("transport refused the message")


@override_settings(EMAIL_BACKEND="glitchtip.test_adapters.FailingEmailBackend")
class SignupSurvivesEmailFailureTestCase(TestCase):
    """The confirmation mail is sent inline during signup. A failing mail
    transport used to turn that into a 500 that still left the account
    behind, so the user saw an error and could not retry the same address."""

    signup_url = "/_allauth/browser/v1/auth/signup"
    data = {"email": "newuser@example.com", "password": "hunter222"}

    def test_signup_succeeds_when_the_confirmation_cannot_be_sent(self):
        res = self.client.post(
            self.signup_url, self.data, content_type="application/json"
        )
        self.assertEqual(res.status_code, 200)
        address = EmailAddress.objects.get(email="newuser@example.com")
        self.assertFalse(address.verified)

    def test_the_failure_is_logged_not_swallowed_silently(self):
        # Test settings disable logging below ERROR globally; this is an
        # ERROR, so it is still captured.
        with self.assertLogs("glitchtip.adapters", level=logging.ERROR) as logs:
            self.client.post(
                self.signup_url, self.data, content_type="application/json"
            )
        self.assertIn("email confirmation", logs.output[0])
        self.assertIn("transport refused the message", "\n".join(logs.output))

    def test_the_new_user_can_sign_in_without_verifying(self):
        self.client.post(self.signup_url, self.data, content_type="application/json")
        self.client.logout()
        res = self.client.post(
            "/_allauth/browser/v1/auth/login",
            self.data,
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
