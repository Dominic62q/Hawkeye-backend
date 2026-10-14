from django.test import TestCase
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
