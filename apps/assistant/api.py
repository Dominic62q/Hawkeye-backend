import asyncio
import codecs
import json
import os
import uuid
from datetime import datetime
from typing import Any, Literal

import aiohttp
from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import Count, QuerySet
from django.http import StreamingHttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Router, Schema
from ninja.errors import HttpError

from apps.organizations_ext.constants import ROLES
from apps.organizations_ext.models import Organization, OrganizationUser
from glitchtip.api.permissions import OrgScopedHttpRequest, org_scoped

from .models import AssistantMessage, AssistantThread, OrganizationAssistantSettings
from .tools import TOOLS, execute_tool

router = Router()

DEFAULT_OPENCODE_URL = "https://opencode.ai/zen/go/v1/chat/completions"
DEFAULT_OPENCODE_MODEL = "deepseek-v4.1-flash"
MAX_TOOL_ROUNDS = 5


class ChatMessage(Schema):
    role: Literal["user", "assistant"]
    content: str


class ChatContext(Schema):
    project: str | None = None
    projectName: str | None = None
    route: str | None = None
    issueId: str | None = None
    transactionId: str | None = None
    monitorId: str | None = None


class ChatRequest(Schema):
    messages: list[ChatMessage]
    threadId: uuid.UUID | None = None
    sessionId: str | None = None
    context: ChatContext | None = None


class ChatResponse(Schema):
    reply: str
    toolsUsed: list[str]
    model: str
    sessionId: str
    threadId: uuid.UUID


class ThreadSummary(Schema):
    id: uuid.UUID
    title: str
    projectName: str
    updatedAt: datetime
    messageCount: int


class ThreadDetail(Schema):
    id: uuid.UUID
    title: str
    projectName: str
    sessionId: str
    updatedAt: datetime
    messages: list[ChatMessage]


class AssistantSettingsSchema(Schema):
    enabled: bool
    scrubPii: bool


async def _org_assistant_settings(org_id: int) -> OrganizationAssistantSettings:
    obj, _ = await OrganizationAssistantSettings.objects.aget_or_create(
        organization_id=org_id,
        defaults={"enabled": True, "scrub_pii": True},
    )
    return obj


async def _require_enabled(org_id: int) -> OrganizationAssistantSettings:
    cfg = await _org_assistant_settings(org_id)
    if not cfg.enabled:
        raise HttpError(403, "AI Assistant is disabled for this organization.")
    return cfg


def _user_id(request: OrgScopedHttpRequest) -> int:
    uid = getattr(request.auth, "user_id", None) or getattr(request.auth, "id", None)
    if not uid:
        raise HttpError(401, "Authentication required.")
    return int(uid)


def _thread_title(text: str) -> str:
    clean = " ".join(text.split())
    return clean[:117] + "…" if len(clean) > 120 else (clean or "New conversation")


TOOL_SCOPES = {
    "list_projects": {"project:read"},
    "list_alerts": {"project:read", "project:write", "project:admin"},
    "list_monitors": {"org:read"},
    "list_issues": {"event:read", "event:write", "event:admin"},
    "get_issue": {"event:read", "event:write", "event:admin"},
    "list_transaction_groups": {"event:read", "event:write", "event:admin"},
    "detect_n_plus_one": {"event:read", "event:write", "event:admin"},
    "list_logs": {"event:read", "event:write", "event:admin"},
}


async def _allowed_tools(request, org_id: int) -> set[str]:
    role = (
        await OrganizationUser.objects.filter(
            user_id=request.auth.user_id,
            organization_id=org_id,
            organization__is_deleted=False,
        )
        .values_list("role", flat=True)
        .afirst()
    )
    if role is None:
        return set()
    scopes = ROLES[role]["scopes"]
    if request.auth.auth_type == "token":
        scopes = scopes & set(request.auth.data.get_scopes())
    return {name for name, required in TOOL_SCOPES.items() if scopes & required}


async def _require_history_access(request, org_id: int) -> None:
    """Saved answers may quote any tool, so reading them needs every tool's scope."""
    if await _allowed_tools(request, org_id) != set(TOOL_SCOPES):
        raise HttpError(
            403, "Saved conversations need access to all assistant data sources."
        )


@sync_to_async
def _save_turn(thread, latest_user, reply_text, session_id):
    with transaction.atomic():
        if not thread._state.adding:
            thread = AssistantThread.objects.select_for_update().get(
                id=thread.id,
                organization_id=thread.organization_id,
                user_id=thread.user_id,
            )
        thread.session_id = session_id
        thread.save(
            update_fields=["session_id", "updated"]
            if not thread._state.adding
            else None
        )
        if latest_user.role == "user":
            AssistantMessage.objects.create(
                thread=thread, role="user", content=latest_user.content
            )
        AssistantMessage.objects.create(
            thread=thread, role="assistant", content=reply_text
        )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _upstream_events(content):
    """Decode SSE independently of network chunk and UTF-8 boundaries."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    pending = ""
    data_lines = []
    async for chunk in content.iter_any():
        pending += decoder.decode(chunk)
        while "\n" in pending or "\r" in pending:
            positions = [
                pos for pos in (pending.find("\n"), pending.find("\r")) if pos >= 0
            ]
            end = min(positions)
            if pending[end] == "\r" and end + 1 == len(pending):
                break
            line = pending[:end]
            delimiter_length = 2 if pending[end : end + 2] == "\r\n" else 1
            pending = pending[end + delimiter_length :]
            if not line:
                if data_lines:
                    yield "\n".join(data_lines)
                    data_lines = []
            elif line.startswith("data:"):
                data_lines.append(line[5:].removeprefix(" "))
            elif line == "data":
                data_lines.append("")
    pending += decoder.decode(b"", final=True)
    if pending or data_lines:
        raise ValueError("Incomplete upstream event")


async def _stream_completion(http, endpoint, headers, body):
    async with http.post(endpoint, json=body, headers=headers) as response:
        if response.status >= 400:
            raise ValueError("Upstream request failed")
        done = False
        async for event in _upstream_events(response.content):
            if event == "[DONE]":
                done = True
                break
            data = json.loads(event)
            if data.get("error"):
                raise ValueError("Upstream request failed")
            yield data
        if not done:
            raise ValueError("Incomplete upstream response")


def _build_system_prompt(org_slug: str, ctx: ChatContext | None) -> str:
    lines = [
        "You are Hawkeye Assistant, a concise, senior engineering debugger embedded inside Hawkeye (a Sentry-compatible error tracking, performance, logs, and uptime platform).",
        f"Active organization: {org_slug}.",
    ]
    if ctx:
        if ctx.projectName or ctx.project:
            lines.append(
                f"Active project filter: {ctx.projectName or ctx.project} (id/slug: {ctx.project})."
            )
        else:
            lines.append("Active project filter: All projects.")
        if ctx.route:
            lines.append(f"Current UI route: {ctx.route}")
        if ctx.issueId:
            lines.append(f"User is currently viewing issue ID: {ctx.issueId}")
        if ctx.transactionId:
            lines.append(
                f"User is currently viewing transaction group ID: {ctx.transactionId}"
            )
        if ctx.monitorId:
            lines.append(f"User is currently viewing monitor ID: {ctx.monitorId}")

    lines.extend(
        [
            "Always use your tools to inspect real telemetry before answering questions about errors, stack traces, performance, N+1 queries, logs, or monitors.",
            "Be direct and technical: cite exact error types, culprits, file paths, p95 latencies, or log timestamps you observed from tool outputs. Never invent telemetry.",
            "Never mention internal tool or function names (such as list_issues, list_monitors, get_issue, etc.) in your response; refer to the underlying data directly.",
            "Keep responses medium-length and focused: lead with the direct answer, use a short bullet list or compact table (at most 3-4 columns) for supporting facts, and include a brief code block only when suggesting a concrete fix. Skip generic preambles and unnecessary follow-up lists. Do not use emojis.",
        ]
    )
    return "\n".join(lines)


@router.get(
    "organizations/{organization_id_or_slug}/assistant/settings/",
    response=AssistantSettingsSchema,
)
@org_scoped(["org:read", "org:write", "org:admin"], Organization)
async def get_assistant_settings(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Organization],
    organization_id_or_slug: str,
):
    organization = await aget_object_or_404(qs.only("id"))
    cfg = await _org_assistant_settings(organization.id)
    return AssistantSettingsSchema(enabled=cfg.enabled, scrubPii=cfg.scrub_pii)


@router.put(
    "organizations/{organization_id_or_slug}/assistant/settings/",
    response=AssistantSettingsSchema,
)
@org_scoped(["org:write", "org:admin"], Organization)
async def update_assistant_settings(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Organization],
    organization_id_or_slug: str,
    payload: AssistantSettingsSchema,
):
    organization = await aget_object_or_404(qs.only("id"))
    cfg = await _org_assistant_settings(organization.id)
    cfg.enabled = payload.enabled
    cfg.scrub_pii = payload.scrubPii
    await cfg.asave(update_fields=["enabled", "scrub_pii", "updated"])
    return AssistantSettingsSchema(enabled=cfg.enabled, scrubPii=cfg.scrub_pii)


@router.get(
    "organizations/{organization_id_or_slug}/assistant/threads/",
    response=list[ThreadSummary],
)
@org_scoped(["event:read", "event:write", "event:admin"], Organization)
async def list_threads(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Organization],
    organization_id_or_slug: str,
):
    organization = await aget_object_or_404(qs.only("id"))
    await _require_enabled(organization.id)
    await _require_history_access(request, organization.id)
    uid = _user_id(request)
    rows = (
        AssistantThread.objects.filter(organization_id=organization.id, user_id=uid)
        .annotate(message_count=Count("messages"))
        .order_by("-updated")[:30]
    )
    return [
        ThreadSummary(
            id=t.id,
            title=t.title,
            projectName=t.project_name,
            updatedAt=t.updated,
            messageCount=t.message_count,
        )
        async for t in rows
    ]


@router.get(
    "organizations/{organization_id_or_slug}/assistant/threads/{thread_id}/",
    response=ThreadDetail,
)
@org_scoped(["event:read", "event:write", "event:admin"], Organization)
async def get_thread(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Organization],
    organization_id_or_slug: str,
    thread_id: uuid.UUID,
):
    organization = await aget_object_or_404(qs.only("id"))
    await _require_history_access(request, organization.id)
    uid = _user_id(request)
    thread = await aget_object_or_404(
        AssistantThread.objects.filter(
            id=thread_id, organization_id=organization.id, user_id=uid
        )
    )
    msgs = [
        ChatMessage(role="user" if m.role == "user" else "assistant", content=m.content)
        async for m in AssistantMessage.objects.filter(thread=thread).order_by(
            "created", "id"
        )
    ]
    return ThreadDetail(
        id=thread.id,
        title=thread.title,
        projectName=thread.project_name,
        sessionId=thread.session_id,
        updatedAt=thread.updated,
        messages=msgs,
    )


@router.delete(
    "organizations/{organization_id_or_slug}/assistant/threads/{thread_id}/",
    response={204: None},
)
@org_scoped(["event:read", "event:write", "event:admin"], Organization)
async def delete_thread(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Organization],
    organization_id_or_slug: str,
    thread_id: uuid.UUID,
):
    organization = await aget_object_or_404(qs.only("id"))
    uid = _user_id(request)
    thread = await aget_object_or_404(
        AssistantThread.objects.filter(
            id=thread_id, organization_id=organization.id, user_id=uid
        )
    )
    await thread.adelete()
    return 204, None


@router.delete(
    "organizations/{organization_id_or_slug}/assistant/threads/",
    response={204: None},
)
@org_scoped(["event:read", "event:write", "event:admin"], Organization)
async def clear_threads(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Organization],
    organization_id_or_slug: str,
):
    organization = await aget_object_or_404(qs.only("id"))
    uid = _user_id(request)
    await AssistantThread.objects.filter(
        organization_id=organization.id, user_id=uid
    ).adelete()
    return 204, None


@router.post(
    "organizations/{organization_id_or_slug}/assistant/chat/",
    response=ChatResponse,
)
@org_scoped(["event:read", "event:write", "event:admin"], Organization)
async def assistant_chat(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Organization],
    organization_id_or_slug: str,
    payload: ChatRequest,
):
    api_key = os.environ.get("OPENCODE_API_KEY", "").strip()
    if not api_key:
        raise HttpError(503, "Assistant is not configured (missing OPENCODE_API_KEY).")
    if not payload.messages:
        raise HttpError(400, "At least one message is required.")

    organization = await aget_object_or_404(qs.only("id", "slug", "name"))
    cfg = await _require_enabled(organization.id)
    uid = _user_id(request)
    allowed_tools = await _allowed_tools(request, organization.id)
    available_tools = [
        tool for tool in TOOLS if tool["function"]["name"] in allowed_tools
    ]

    endpoint = os.environ.get("OPENCODE_API_URL", DEFAULT_OPENCODE_URL).strip()
    model = os.environ.get("OPENCODE_MODEL", DEFAULT_OPENCODE_MODEL).strip()

    if payload.threadId:
        thread = await aget_object_or_404(
            AssistantThread.objects.filter(
                id=payload.threadId, organization_id=organization.id, user_id=uid
            )
        )
        session_id = (
            thread.session_id
            or f"hawkeye-o{organization.id}-u{uid}-{thread.id.hex[:8]}"
        )
    else:
        first_user = next((m.content for m in payload.messages if m.role == "user"), "")
        thread = AssistantThread(
            organization_id=organization.id,
            user_id=uid,
            title=_thread_title(first_user),
            project_name=(
                payload.context.projectName
                if payload.context and payload.context.projectName
                else "All projects"
            ),
            session_id="",
        )
        session_id = f"hawkeye-o{organization.id}-u{uid}-{thread.id.hex[:8]}"

    latest_user = payload.messages[-1]

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": _build_system_prompt(organization.slug, payload.context),
        },
        *[{"role": m.role, "content": m.content} for m in payload.messages[-20:]],
    ]

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "Hawkeye-Assistant/1.0",
        "x-opencode-session": session_id,
    }

    async def stream():
        tools_used: list[str] = []
        used_model = model
        try:
            yield _sse("status", {"message": "Thinking…"})
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=45)
            ) as http:
                for _ in range(MAX_TOOL_ROUNDS):
                    body = {
                        "model": model,
                        "messages": messages,
                        "tools": available_tools,
                        "stream": True,
                    }
                    content_parts = []
                    calls = {}
                    finish_reason = None
                    async for data in _stream_completion(http, endpoint, headers, body):
                        used_model = data.get("model") or used_model
                        choices = data.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        delta = choice.get("delta") or {}
                        if choice.get("finish_reason") is not None:
                            finish_reason = choice["finish_reason"]
                        fragments = delta.get("tool_calls") or []
                        if fragments and not calls and content_parts:
                            yield _sse("reset", {})
                        for fragment in fragments:
                            index = fragment["index"]
                            call = calls.setdefault(
                                index,
                                {
                                    "id": "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                },
                            )
                            call["id"] += fragment.get("id") or ""
                            fn = fragment.get("function") or {}
                            call["function"]["name"] += fn.get("name") or ""
                            call["function"]["arguments"] += fn.get("arguments") or ""
                        text = delta.get("content") or ""
                        if text and not calls:
                            content_parts.append(text)
                            yield _sse("text", {"text": text})

                    if calls:
                        if finish_reason != "tool_calls":
                            raise ValueError("Incomplete tool call")
                        tool_calls = [calls[index] for index in sorted(calls)]
                        messages.append(
                            {
                                "role": "assistant",
                                "content": "".join(content_parts),
                                "tool_calls": tool_calls,
                            }
                        )
                        yield _sse("status", {"message": "Running tools…"})
                        for tc in tool_calls:
                            fn = tc["function"]
                            fn_name = fn["name"]
                            try:
                                fn_args = json.loads(fn["arguments"] or "{}")
                            except json.JSONDecodeError:
                                fn_args = {}
                            if fn_name and fn_name not in tools_used:
                                tools_used.append(fn_name)
                            if fn_name in allowed_tools:
                                result = await execute_tool(
                                    uid,
                                    organization.slug,
                                    fn_name,
                                    fn_args,
                                    scrub_pii=cfg.scrub_pii,
                                )
                            else:
                                result = json.dumps({"error": "Tool not permitted."})
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tc["id"],
                                    "content": result,
                                }
                            )
                        yield _sse("status", {"message": "Thinking…"})
                        continue
                    if finish_reason != "stop":
                        raise ValueError("Incomplete upstream response")
                    reply_text = "".join(content_parts)
                    break
                else:
                    reply_text = (
                        "I gathered telemetry across multiple steps but hit the tool-call limit. "
                        "Try narrowing your question to a specific issue, endpoint, or project."
                    )
                    yield _sse("text", {"text": reply_text})

            await _save_turn(thread, latest_user, reply_text, session_id)
            yield _sse(
                "done",
                {
                    "reply": reply_text,
                    "toolsUsed": tools_used,
                    "model": used_model,
                    "sessionId": session_id,
                    "threadId": str(thread.id),
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            yield _sse(
                "error", {"message": "Assistant response failed. Please try again."}
            )

    response = StreamingHttpResponse(stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response
