import json
import re
from typing import Any
from urllib.parse import urlsplit

from apps.mcp import data as mcp_data
from apps.mcp import serializers as mcp_serializers

REDACTED_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
    }
)

PII_KEYS = frozenset({"email", "ip_address", "username"})

_RE_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_RE_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_RE_AUTH = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._\-+/=]{8,}", re.IGNORECASE)
_RE_CONN = re.compile(
    r"\b(?:postgres(?:ql)?|redis|rediss|mysql|mongodb|amqp)://[^\s\"'<>]+",
    re.IGNORECASE,
)
_RE_QS_SECRET = re.compile(
    r"([?&](?:token|key|secret|password|code|api_key|access_token|refresh_token)=)[^&\s\"'<>]+",
    re.IGNORECASE,
)
_RE_WEBHOOK = re.compile(
    r"\bhttps?://(?:hooks\.slack\.com|(?:[\w-]+\.)?discord(?:app)?\.com/api/webhooks"
    r"|chat\.googleapis\.com|[\w.-]*\.webhook\.office\.com|outlook\.office\.com/webhook)"
    r"[^\s\"'<>]*",
    re.IGNORECASE,
)
_RE_CREDENTIAL = re.compile(
    r"""(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?token|refresh[_-]?token)\b(["']?\s*[:=]\s*["']?)([^'"\\\s,;&}]+)"""
)


def _sensitive_key(key: str, scrub_pii: bool) -> bool:
    key = key.lower().replace("-", "_")
    return key in {k.replace("-", "_") for k in REDACTED_KEYS} or (
        scrub_pii and key in PII_KEYS
    )


def _scrub_text(text: str, *, scrub_pii: bool) -> str:
    text = _RE_CONN.sub("[CONN_STRING]", text)
    text = _RE_WEBHOOK.sub("[WEBHOOK_URL]", text)
    text = _RE_AUTH.sub(r"\1 [REDACTED]", text)
    text = _RE_QS_SECRET.sub(r"\1[REDACTED]", text)
    text = _RE_CREDENTIAL.sub(r"\1\2[REDACTED]", text)
    if scrub_pii:
        text = _RE_EMAIL.sub("[EMAIL]", text)
        text = _RE_IPV4.sub("[IP]", text)
    return text


def _mask_recipient_urls(alerts: list[dict[str, Any]]) -> None:
    """Alert recipient URLs are webhook endpoints whose path is the secret."""
    for alert in alerts:
        for recipient in alert.get("alertRecipients", []):
            url = recipient.get("url") or ""
            parts = urlsplit(url)
            if parts.scheme and parts.netloc:
                recipient["url"] = f"{parts.scheme}://{parts.netloc}/[REDACTED]"
            elif url:
                recipient["url"] = "[REDACTED]"


def _redact(value: Any, *, scrub_pii: bool = True, depth: int = 0) -> Any:
    """Best-effort scrub; arbitrary telemetry can still contain sensitive content."""
    if depth > 8:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        out = {}
        tag_key = value.get("key")
        sensitive_tag = isinstance(tag_key, str) and _sensitive_key(tag_key, scrub_pii)
        for k, v in value.items():
            if _sensitive_key(str(k), scrub_pii) or (sensitive_tag and k == "value"):
                out[k] = "[REDACTED]"
            else:
                out[k] = _redact(v, scrub_pii=scrub_pii, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [
            _redact(item, scrub_pii=scrub_pii, depth=depth + 1) for item in value[:50]
        ]
    if isinstance(value, str):
        if len(value) > 4000:
            value = value[:4000] + "…[truncated]"
        return _scrub_text(value, scrub_pii=scrub_pii)
    return value


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_projects",
            "description": "List all projects in the current organization.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_issues",
            "description": "List issues (errors) in the organization, optionally filtered by project slug or search query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_slug": {
                        "type": "string",
                        "description": "Optional project slug to filter by.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query, e.g. 'is:unresolved' or error text.",
                    },
                    "sort": {
                        "type": "string",
                        "enum": ["-last_seen", "-first_seen", "-count", "-priority"],
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_issue",
            "description": "Get full details and the latest event (including stack trace, culprit, tags, and breadcrumbs) for an issue ID or shortId.",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_id": {
                        "type": "string",
                        "description": "Numeric issue ID or shortId (e.g. 'PROJECT-12').",
                    },
                },
                "required": ["issue_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_transaction_groups",
            "description": "List performance transaction groups (endpoints/jobs) with p50, p95, throughput, and error rate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_slug": {"type": "string"},
                    "query": {"type": "string"},
                    "sort": {
                        "type": "string",
                        "enum": ["-avg_duration", "-count", "-created"],
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detect_n_plus_one",
            "description": "Detect repeated N+1 database/span query patterns in a project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_slug": {"type": "string"},
                    "min_occurrences": {"type": "integer", "minimum": 2, "maximum": 50},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_monitors",
            "description": "List uptime monitors and their current up/down status.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_logs",
            "description": "Search recent application logs in the organization.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_slug": {"type": "string"},
                    "query": {"type": "string"},
                    "level": {
                        "type": "string",
                        "description": "Comma-separated levels like 'error,fatal' or 'warn'.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 30},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_alerts",
            "description": "List configured alert rules in the organization.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_slug": {"type": "string"},
                },
            },
        },
    },
]


async def _resolve_project_id(
    user_id: int,
    org_slug: str,
    project_slug: str | None,
) -> int | None:
    if project_slug is None:
        return None
    projects = await mcp_data.get_projects(user_id, org_slug)
    match = next(
        (p for p in projects if p.slug == project_slug or str(p.id) == project_slug),
        None,
    )
    if match is None:
        raise ValueError(f"Project '{project_slug}' not found.")
    return match.id


async def execute_tool(
    user_id: int,
    org_slug: str,
    name: str,
    arguments: dict[str, Any],
    *,
    scrub_pii: bool = True,
) -> str:
    """Execute a read-only Hawkeye telemetry tool scoped to the authenticated user."""
    try:
        if arguments.get("project_slug") is not None and name in {
            "list_issues",
            "list_alerts",
        }:
            await _resolve_project_id(user_id, org_slug, arguments["project_slug"])
        if name == "list_projects":
            projects = await mcp_data.get_projects(user_id, org_slug)
            payload = [mcp_serializers.serialize_project(p) for p in projects]

        elif name == "list_issues":
            issues = await mcp_data.get_issues(
                user_id,
                org_slug,
                project_slug=arguments.get("project_slug"),
                query=arguments.get("query") or "is:unresolved",
                sort=arguments.get("sort") or "-last_seen",
                limit=min(int(arguments.get("limit") or 15), 25),
            )
            payload = [mcp_serializers.serialize_issue(i) for i in issues]

        elif name == "get_issue":
            issue_id = str(arguments["issue_id"])
            resolved_id = await mcp_data.resolve_issue_id(user_id, issue_id, org_slug)
            issue = (
                await mcp_data.get_issue(user_id, resolved_id)
                if resolved_id is not None
                else None
            )
            if not issue or issue.project.organization_id != await mcp_data._get_org_id(
                user_id, org_slug
            ):
                return json.dumps({"error": "Issue not found."})
            event = await mcp_data.get_latest_event(user_id, issue.id)
            if event and event.organization_id != issue.project.organization_id:
                event = None
            payload = {
                "issue": mcp_serializers.serialize_issue(issue),
                "latestEvent": mcp_serializers.serialize_event(event)
                if event
                else None,
            }

        elif name == "list_transaction_groups":
            project_id = await _resolve_project_id(
                user_id, org_slug, arguments.get("project_slug")
            )
            groups = await mcp_data.get_transaction_groups(
                user_id,
                org_slug,
                project_ids=[project_id] if project_id else None,
                query=arguments.get("query"),
                sort=arguments.get("sort") or "-avg_duration",
                limit=min(int(arguments.get("limit") or 15), 25),
            )
            payload = [mcp_serializers.serialize_transaction_group(g) for g in groups]

        elif name == "detect_n_plus_one":
            project_id = await _resolve_project_id(
                user_id, org_slug, arguments.get("project_slug")
            )
            patterns = await mcp_data.get_n_plus_one_patterns(
                user_id,
                org_slug,
                project_ids=[project_id] if project_id else None,
                threshold=float(arguments.get("min_occurrences") or 3),
                limit=15,
            )
            payload = [
                mcp_serializers.serialize_n_plus_one_pattern(p) for p in patterns
            ]

        elif name == "list_monitors":
            monitors = await mcp_data.get_monitors(user_id, org_slug)
            payload = [mcp_serializers.serialize_monitor(m) for m in monitors]

        elif name == "list_logs":
            project_id = await _resolve_project_id(
                user_id, org_slug, arguments.get("project_slug")
            )
            logs = await mcp_data.get_logs(
                user_id,
                org_slug,
                project_id=project_id,
                query=arguments.get("query"),
                level=arguments.get("level"),
                limit=min(int(arguments.get("limit") or 20), 30),
            )
            payload = [mcp_serializers.serialize_log_event(log) for log in logs]

        elif name == "list_alerts":
            alerts = await mcp_data.get_alerts(
                user_id,
                org_slug,
                project_slug=arguments.get("project_slug"),
            )
            payload = [mcp_serializers.serialize_alert(a) for a in alerts]
            _mask_recipient_urls(payload)

        else:
            return json.dumps({"error": f"Unknown tool: {name}"})

        return json.dumps(_redact(payload, scrub_pii=scrub_pii), default=str)
    except Exception as exc:
        return json.dumps(
            {"error": _scrub_text(f"Tool {name} failed: {exc}", scrub_pii=scrub_pii)}
        )
