import logging

from django.test import Client, TestCase
from django.utils import timezone
from model_bakery import baker

from apps.api_tokens.models import APIToken
from apps.organizations_ext.constants import OrganizationUserRole
from glitchtip.test_utils import generators  # noqa: F401

OVERVIEW = "/api/0/operator/overview/"
ORGS = "/api/0/operator/organizations/"
USERS = "/api/0/operator/users/"

SECRET_TITLE = "KeyError: maya@customer.example"


class OperatorAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = baker.make("users.user", is_superuser=True, is_active=True)
        cls.member = baker.make("users.user", email="member@customer.example")
        cls.project = baker.make("projects.Project", name="checkout")
        cls.organization = cls.project.organization
        cls.organization.add_user(cls.member, OrganizationUserRole.OWNER)
        baker.make(
            "projects.IssueEventProjectHourlyStatistic",
            project=cls.project,
            organization=cls.organization,
            date=timezone.now(),
            count=7,
        )
        baker.make(
            "issue_events.Issue",
            project=cls.project,
            title=SECRET_TITLE,
            culprit="orders.checkout",
            metadata={"value": "secret-metadata-value"},
        )

    def test_requires_login(self):
        for url in (OVERVIEW, ORGS, USERS, f"{ORGS}{self.organization.id}/"):
            self.assertEqual(self.client.get(url).status_code, 401, url)

    def test_rejects_ordinary_user(self):
        self.client.force_login(self.member)
        for url in (OVERVIEW, ORGS, USERS, f"{ORGS}{self.organization.id}/"):
            self.assertEqual(self.client.get(url).status_code, 401, url)

    def test_rejects_inactive_superuser(self):
        self.superuser.is_active = False
        self.superuser.save()
        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get(OVERVIEW).status_code, 401)

    def test_rejects_api_token_of_a_superuser(self):
        token = baker.make(APIToken, user=self.superuser)
        res = self.client.get(
            OVERVIEW, headers={"Authorization": f"Bearer {token.token}"}
        )
        self.assertEqual(res.status_code, 401)

    def test_overview(self):
        self.client.force_login(self.superuser)
        res = self.client.get(OVERVIEW)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["organizations"]["total"], 1)
        self.assertEqual(data["users"]["superusers"], 1)
        self.assertEqual(data["projects"], 1)
        self.assertEqual(data["usage"]["issueEvents"]["last24Hours"], 7)
        self.assertEqual(data["usage"]["issueEvents"]["last30Days"], 7)

    def test_organization_list_and_search(self):
        self.client.force_login(self.superuser)
        data = self.client.get(ORGS).json()
        self.assertEqual(data["count"], 1)
        row = data["results"][0]
        self.assertEqual(row["id"], self.organization.id)
        self.assertEqual(row["memberCount"], 1)
        self.assertEqual(row["projectCount"], 1)
        self.assertEqual(row["events30Days"], 7)
        self.assertTrue(row["isAcceptingEvents"])

        self.assertEqual(self.client.get(ORGS, {"q": "no-such-org"}).json()["count"], 0)
        hit = self.client.get(ORGS, {"q": self.organization.name[:4]}).json()
        self.assertEqual(hit["count"], 1)

    def test_organization_detail(self):
        self.client.force_login(self.superuser)
        res = self.client.get(f"{ORGS}{self.organization.id}/")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["usage"]["issueEvents"], 7)
        self.assertEqual(data["members"][0]["email"], "member@customer.example")
        self.assertEqual(data["members"][0]["role"], "Owner")
        self.assertEqual(data["projects"][0]["name"], "checkout")

    def test_unknown_organization_is_404(self):
        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get(f"{ORGS}999999/").status_code, 404)

    def test_user_list_and_search(self):
        self.client.force_login(self.superuser)
        data = self.client.get(USERS, {"q": "member@"}).json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["organizationCount"], 1)
        self.assertFalse(data["results"][0]["isSuperuser"])

    def test_never_returns_content_or_credentials(self):
        token = baker.make(APIToken, user=self.member)
        self.client.force_login(self.superuser)
        body = "".join(
            self.client.get(url).content.decode()
            for url in (OVERVIEW, ORGS, USERS, f"{ORGS}{self.organization.id}/")
        )
        for secret in (
            "maya@customer.example",
            "orders.checkout",
            "secret-metadata-value",
            token.token,
            "password",
        ):
            self.assertNotIn(secret, body)


def ingest_url(org_id):
    return f"{ORGS}{org_id}/ingest/"


def active_url(user_id):
    return f"{USERS}{user_id}/active/"


class OperatorActionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = baker.make("users.user", is_superuser=True, is_active=True)
        cls.victim = baker.make("users.user", email="victim@customer.example")
        cls.project = baker.make("projects.Project")
        cls.organization = cls.project.organization
        cls.organization.add_user(cls.victim, OrganizationUserRole.OWNER)

    def post(self, url, body):
        return self.client.post(url, body, content_type="application/json")

    def test_actions_need_a_superuser(self):
        calls = [
            (ingest_url(self.organization.id), {"accepting": False}),
            (active_url(self.victim.id), {"active": False}),
        ]
        for url, body in calls:
            self.assertEqual(self.post(url, body).status_code, 401, url)
        self.client.force_login(self.victim)
        for url, body in calls:
            self.assertEqual(self.post(url, body).status_code, 401, url)
        self.organization.refresh_from_db()
        self.victim.refresh_from_db()
        self.assertTrue(self.organization.is_accepting_events)
        self.assertTrue(self.victim.is_active)

    def test_actions_reject_api_tokens(self):
        token = baker.make(APIToken, user=self.superuser)
        res = self.client.post(
            ingest_url(self.organization.id),
            {"accepting": False},
            content_type="application/json",
            headers={"Authorization": f"Bearer {token.token}"},
        )
        self.assertEqual(res.status_code, 401)

    def test_pause_and_resume_ingest(self):
        self.client.force_login(self.superuser)
        res = self.post(ingest_url(self.organization.id), {"accepting": False})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"isAcceptingEvents": False})
        self.organization.refresh_from_db()
        self.assertFalse(self.organization.is_accepting_events)

        listed = self.client.get(ORGS).json()["results"][0]
        self.assertFalse(listed["isAcceptingEvents"])

        res = self.post(ingest_url(self.organization.id), {"accepting": True})
        self.assertEqual(res.json(), {"isAcceptingEvents": True})
        self.organization.refresh_from_db()
        self.assertTrue(self.organization.is_accepting_events)

    def test_ingest_unknown_organization_is_404(self):
        self.client.force_login(self.superuser)
        res = self.post(ingest_url(999999), {"accepting": False})
        self.assertEqual(res.status_code, 404)

    def test_deactivate_and_reactivate_user(self):
        self.client.force_login(self.superuser)
        res = self.post(active_url(self.victim.id), {"active": False})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"isActive": False})
        self.victim.refresh_from_db()
        self.assertFalse(self.victim.is_active)

        res = self.post(active_url(self.victim.id), {"active": True})
        self.assertEqual(res.json(), {"isActive": True})
        self.victim.refresh_from_db()
        self.assertTrue(self.victim.is_active)

    def test_deactivation_ends_signed_in_api_sessions(self):
        """The API reads the user id from the session without loading the user."""
        victim_client = Client()
        victim_client.force_login(self.victim)
        self.assertEqual(victim_client.get("/api/0/users/me/").status_code, 200)

        self.client.force_login(self.superuser)
        self.post(active_url(self.victim.id), {"active": False})
        self.assertEqual(victim_client.get("/api/0/users/me/").status_code, 401)

        self.post(active_url(self.victim.id), {"active": True})
        self.assertEqual(victim_client.get("/api/0/users/me/").status_code, 200)

    def test_cannot_deactivate_yourself(self):
        self.client.force_login(self.superuser)
        res = self.post(active_url(self.superuser.id), {"active": False})
        self.assertEqual(res.status_code, 403)
        self.superuser.refresh_from_db()
        self.assertTrue(self.superuser.is_active)

    def test_cannot_deactivate_another_superuser(self):
        other = baker.make("users.user", is_superuser=True, is_active=True)
        self.client.force_login(self.superuser)
        res = self.post(active_url(other.id), {"active": False})
        self.assertEqual(res.status_code, 403)
        other.refresh_from_db()
        self.assertTrue(other.is_active)

    def test_unknown_user_is_404(self):
        self.client.force_login(self.superuser)
        self.assertEqual(
            self.post(active_url(999999), {"active": False}).status_code, 404
        )

    def test_actions_are_logged_with_the_actor(self):
        # settings.py globally disables WARNING-and-below while testing.
        previous_disable = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, previous_disable)
        self.client.force_login(self.superuser)
        with self.assertLogs("apps.operator.api", level="WARNING") as logs:
            self.post(ingest_url(self.organization.id), {"accepting": False})
            self.post(active_url(self.victim.id), {"active": False})
        text = "\n".join(logs.output)
        self.assertIn(f"user {self.superuser.id}", text)
        self.assertIn(f"organization {self.organization.id}", text)
        self.assertIn(f"user {self.victim.id}", text)
