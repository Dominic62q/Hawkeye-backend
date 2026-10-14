"""
Read-only metadata for the operator console.

Everything here is superuser-only and session-only (no API tokens). It exposes who
exists, how much they use and whether ingest is accepted. It never returns event
content: no titles, stack traces, request data, logs or tokens.
"""

from datetime import datetime, timedelta
from typing import Any

from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce, Collate
from django.http import HttpRequest
from django.shortcuts import aget_object_or_404
from django.utils import timezone
from ninja import Query, Router

from apps.organizations_ext.constants import OrganizationUserRole
from apps.organizations_ext.models import (
    Organization,
    OrganizationUser,
    get_current_period_dates,
    get_event_counts,
)
from apps.projects.models import (
    IssueEventProjectHourlyStatistic,
    LogProjectHourlyStatistic,
    Project,
    TransactionEventProjectHourlyStatistic,
)
from apps.uptime.models import UptimeCheckHourlyStatistic
from apps.users.models import User
from glitchtip.api.authentication import Auth, SessionAuth

from .schema import (
    OperatorOrganizationDetailSchema,
    OperatorOrganizationPage,
    OperatorOverviewSchema,
    OperatorUserPage,
    PageQuery,
)

PAGE_SIZE = 25
USAGE_WINDOW_DAYS = 30

# Hourly statistic tables, keyed by the name shown in the console.
USAGE_TABLES: dict[str, Any] = {
    "issueEvents": IssueEventProjectHourlyStatistic,
    "transactions": TransactionEventProjectHourlyStatistic,
    "logs": LogProjectHourlyStatistic,
    "uptimeChecks": UptimeCheckHourlyStatistic,
}


class OperatorAuth(SessionAuth):
    """A logged-in, active superuser. Tokens never qualify, so a leaked one cannot read this."""

    async def authenticate(self, request: HttpRequest, key: str | None) -> Auth | None:
        auth = await super().authenticate(request, key)
        if auth is None:
            return None
        is_operator = await User.objects.filter(
            id=auth.user_id, is_active=True, is_superuser=True
        ).aexists()
        return auth if is_operator else None


router = Router(auth=OperatorAuth(), tags=["operator"])


def _window(query: PageQuery) -> tuple[int, int, int]:
    page = max(query.page, 1)
    start = (page - 1) * PAGE_SIZE
    return page, start, start + PAGE_SIZE


def _search(qs, query: PageQuery, *fields: str):
    q = (query.q or "").strip()
    if not q:
        return qs
    condition = Q()
    for field in fields:
        condition |= Q(**{f"{field}__icontains": q})
    return qs.filter(condition)


async def _usage_by_org(org_ids: list[int], since: datetime) -> dict[int, int]:
    """Total events per organization since a date. One query per table, never a join across them."""
    totals = dict.fromkeys(org_ids, 0)
    for model in USAGE_TABLES.values():
        rows = (
            model.objects.filter(organization_id__in=org_ids, date__gte=since)
            .values("organization_id")
            .annotate(total=Coalesce(Sum("count"), 0))
        )
        async for row in rows:
            totals[row["organization_id"]] += row["total"]
    return totals


async def _grouped_counts(model, field: str, ids: list[int]) -> dict[int, int]:
    rows = (
        model.objects.filter(**{f"{field}__in": ids})
        .values(field)
        .annotate(total=Count("id"))
    )
    return {row[field]: row["total"] async for row in rows}


@router.get("operator/overview/", response=OperatorOverviewSchema, by_alias=True)
async def overview(request: HttpRequest):
    now = timezone.now()
    month_ago = now - timedelta(days=USAGE_WINDOW_DAYS)
    day_ago = now - timedelta(days=1)

    orgs = await Organization.objects.filter(is_deleted=False).aaggregate(
        total=Count("id"),
        active=Count("id", filter=Q(is_active=True)),
        paused=Count("id", filter=Q(is_accepting_events=False)),
        throttled=Count("id", filter=Q(event_throttle_rate__gt=0)),
        new=Count("id", filter=Q(created__gte=month_ago)),
    )
    users = await User.objects.aaggregate(
        total=Count("id"),
        active=Count("id", filter=Q(is_active=True)),
        superusers=Count("id", filter=Q(is_superuser=True)),
        new=Count("id", filter=Q(created__gte=month_ago)),
        recent=Count("id", filter=Q(last_login__gte=month_ago)),
    )

    usage: dict[str, dict[str, int]] = {}
    for name, model in USAGE_TABLES.items():
        row = await model.objects.filter(date__gte=month_ago).aaggregate(
            month=Coalesce(Sum("count"), 0),
            day=Coalesce(Sum("count", filter=Q(date__gte=day_ago)), 0),
        )
        usage[name] = {"last30Days": row["month"], "last24Hours": row["day"]}

    return {
        "organizations": orgs,
        "users": users,
        "projects": await Project.objects.acount(),
        "usage": usage,
        "usageWindowDays": USAGE_WINDOW_DAYS,
    }


@router.get("operator/organizations/", response=OperatorOrganizationPage, by_alias=True)
async def list_organizations(request: HttpRequest, query: Query[PageQuery]):
    page, start, end = _window(query)
    qs = _search(
        Organization.objects.filter(is_deleted=False), query, "name", "slug"
    ).order_by("-created")
    total = await qs.acount()
    orgs = [org async for org in qs[start:end]]
    ids = [org.id for org in orgs]

    since = timezone.now() - timedelta(days=USAGE_WINDOW_DAYS)
    events = await _usage_by_org(ids, since)
    members = await _grouped_counts(OrganizationUser, "organization_id", ids)
    projects = await _grouped_counts(Project, "organization_id", ids)

    return {
        "count": total,
        "page": page,
        "pageSize": PAGE_SIZE,
        "results": [
            {
                "id": org.id,
                "name": org.name,
                "slug": org.slug,
                "created": org.created,
                "isActive": org.is_active,
                "isAcceptingEvents": org.is_accepting_events,
                "eventThrottleRate": org.event_throttle_rate,
                "memberCount": members.get(org.id, 0),
                "projectCount": projects.get(org.id, 0),
                "events30Days": events[org.id],
            }
            for org in orgs
        ],
    }


@router.get(
    "operator/organizations/{int:org_id}/",
    response=OperatorOrganizationDetailSchema,
    by_alias=True,
)
async def organization_detail(request: HttpRequest, org_id: int):
    org = await aget_object_or_404(Organization, id=org_id, is_deleted=False)

    period = await get_current_period_dates(org)
    start, end = period if period else (None, None)
    counts = await get_event_counts(org.id, start, end)

    members = [
        {
            "id": member.id,
            "email": member.user.email if member.user else member.email,
            "name": member.user.name if member.user else "",
            "role": OrganizationUserRole(member.role).label,
            "pending": member.user is None,
            "lastLogin": member.user.last_login if member.user else None,
            "created": member.created,
        }
        async for member in OrganizationUser.objects.filter(organization_id=org.id)
        .select_related("user")
        .order_by("-role", "created")
    ]
    projects = [
        {
            "id": project.id,
            "name": project.name,
            "slug": project.slug,
            "platform": project.platform,
            "created": project.created,
            "firstEvent": project.first_event,
        }
        async for project in Project.objects.filter(organization_id=org.id).order_by(
            "name"
        )
    ]

    return {
        "id": org.id,
        "name": org.name,
        "slug": org.slug,
        "created": org.created,
        "isActive": org.is_active,
        "isAcceptingEvents": org.is_accepting_events,
        "eventThrottleRate": org.event_throttle_rate,
        "periodStart": start,
        "periodEnd": end,
        "usage": {
            "issueEvents": counts.issue_event_count,
            "transactions": counts.transaction_count,
            "logs": counts.log_count,
            "uptimeChecks": counts.uptime_check_event_count,
            "fileSizeMb": counts.file_size,
            "billedTotal": counts.total_event_count,
        },
        "members": members,
        "projects": projects,
    }


@router.get("operator/users/", response=OperatorUserPage, by_alias=True)
async def list_users(request: HttpRequest, query: Query[PageQuery]):
    page, start, end = _window(query)
    # users.email has a nondeterministic collation, which Postgres cannot use with LIKE.
    qs = _search(
        User.objects.annotate(email_search=Collate("email", "default")),
        query,
        "email_search",
        "name",
    ).order_by("-created")
    total = await qs.acount()
    users = [user async for user in qs[start:end]]
    memberships = await _grouped_counts(
        OrganizationUser, "user_id", [user.id for user in users]
    )

    return {
        "count": total,
        "page": page,
        "pageSize": PAGE_SIZE,
        "results": [
            {
                "id": user.id,
                "email": user.email,
                "name": user.name,
                "isActive": user.is_active,
                "isSuperuser": user.is_superuser,
                "created": user.created,
                "lastLogin": user.last_login,
                "organizationCount": memberships.get(user.id, 0),
            }
            for user in users
        ],
    }
