from datetime import datetime

from ninja import Schema

from glitchtip.schema import CamelSchema


class PageQuery(Schema):
    q: str | None = None
    page: int = 1


class CountsSchema(CamelSchema):
    total: int
    active: int
    new: int


class OperatorOrgCounts(CountsSchema):
    paused: int
    throttled: int


class OperatorUserCounts(CountsSchema):
    superusers: int
    recent: int


class UsageWindow(CamelSchema):
    last_30_days: int
    last_24_hours: int


class OperatorUsage(CamelSchema):
    issue_events: UsageWindow
    transactions: UsageWindow
    logs: UsageWindow
    uptime_checks: UsageWindow


class OperatorOverviewSchema(CamelSchema):
    organizations: OperatorOrgCounts
    users: OperatorUserCounts
    projects: int
    usage: OperatorUsage
    usage_window_days: int


class OperatorOrganizationRow(CamelSchema):
    id: int
    name: str
    slug: str
    created: datetime
    is_active: bool
    is_accepting_events: bool
    event_throttle_rate: int
    member_count: int
    project_count: int
    events_30_days: int


class OperatorOrganizationPage(CamelSchema):
    count: int
    page: int
    page_size: int
    results: list[OperatorOrganizationRow]


class OperatorOrgUsage(CamelSchema):
    issue_events: int
    transactions: int
    logs: int
    uptime_checks: int
    file_size_mb: int
    billed_total: int


class OperatorMember(CamelSchema):
    id: int
    email: str | None
    name: str
    role: str
    pending: bool
    last_login: datetime | None
    created: datetime


class OperatorProject(CamelSchema):
    id: int
    name: str
    slug: str
    platform: str | None
    created: datetime
    first_event: datetime | None


class OperatorOrganizationDetailSchema(CamelSchema):
    id: int
    name: str
    slug: str
    created: datetime
    is_active: bool
    is_accepting_events: bool
    event_throttle_rate: int
    period_start: datetime | None
    period_end: datetime | None
    usage: OperatorOrgUsage
    members: list[OperatorMember]
    projects: list[OperatorProject]


class OperatorUserRow(CamelSchema):
    id: int
    email: str
    name: str
    is_active: bool
    is_superuser: bool
    created: datetime
    last_login: datetime | None
    organization_count: int


class OperatorUserPage(CamelSchema):
    count: int
    page: int
    page_size: int
    results: list[OperatorUserRow]
