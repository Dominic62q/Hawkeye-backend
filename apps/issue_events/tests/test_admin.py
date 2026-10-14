from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from glitchtip.test_utils import generators  # noqa: F401


class IssueAdminTests(TestCase):
    """Issue titles and metadata can hold customer data, so the admin shows neither."""

    def setUp(self):
        self.admin_user = baker.make("users.user", is_staff=True, is_superuser=True)
        self.client.force_login(self.admin_user)
        self.issue = baker.make(
            "issue_events.Issue",
            title="KeyError: maya@customer.example",
            culprit="orders.checkout",
            metadata={"value": "secret-metadata-value"},
        )

    def test_view_hides_content(self):
        res = self.client.get(
            reverse("admin:issue_events_issue_change", args=[self.issue.pk])
        )
        self.assertEqual(res.status_code, 200)
        self.assertNotContains(res, "maya@customer.example")
        self.assertNotContains(res, "orders.checkout")
        self.assertNotContains(res, "secret-metadata-value")

    def test_changelist_hides_content(self):
        res = self.client.get(reverse("admin:issue_events_issue_changelist"))
        self.assertEqual(res.status_code, 200)
        self.assertNotContains(res, "maya@customer.example")

    def test_is_view_only(self):
        self.assertEqual(
            self.client.get(reverse("admin:issue_events_issue_add")).status_code, 403
        )
        res = self.client.post(
            reverse("admin:issue_events_issue_change", args=[self.issue.pk]),
            {"title": "changed"},
        )
        self.assertEqual(res.status_code, 403)
        self.issue.refresh_from_db()
        self.assertNotEqual(self.issue.title, "changed")
