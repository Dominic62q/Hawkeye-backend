import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, TestCase
from model_bakery import baker
from ninja.errors import HttpError

from apps.assistant.api import (
    ChatMessage,
    ChatRequest,
    _allowed_tools,
    _save_turn,
    _upstream_events,
    assistant_chat,
    get_thread,
    list_threads,
)
from apps.assistant.models import AssistantMessage, AssistantThread
from apps.assistant.tools import _redact, execute_tool
from glitchtip.test_utils.issue import make_issue


class ScrubbingTests(SimpleTestCase):
    def test_upstream_parser_handles_multiline_crlf_and_split_unicode(self):
        class Content:
            async def iter_any(self):
                for (
                    byte
                ) in ': comment\r\ndata: {"text":\r\ndata: "é"}\r\n\r\n'.encode():
                    yield bytes([byte])

        async def collect():
            return [json.loads(event) async for event in _upstream_events(Content())]

        self.assertEqual(async_to_sync(collect)(), [{"text": "é"}])

    def test_secrets_are_scrubbed_without_pii_scrubbing(self):
        value = {
            "tags": [{"key": "api_key", "value": "tag-secret"}],
            "message": "password=hunter2 Bearer abcdefgh123 user@example.com",
            "email": "user@example.com",
        }
        result = _redact(value, scrub_pii=False)
        self.assertEqual(result["tags"][0]["value"], "[REDACTED]")
        self.assertNotIn("hunter2", result["message"])
        self.assertNotIn("abcdefgh123", result["message"])
        self.assertIn("user@example.com", result["message"])
        self.assertEqual(result["email"], "user@example.com")

    def test_depth_limit_never_returns_unscrubbed_values(self):
        value = {"token": "deep-secret"}
        for _ in range(12):
            value = {"nested": value}
        self.assertNotIn("deep-secret", json.dumps(_redact(value, scrub_pii=False)))

    def test_alert_webhook_secrets_never_reach_the_model(self):
        secret_url = "https://hooks.slack.com/services/T000/B000/SECRETVALUE"
        recipient = SimpleNamespace(id=1, recipient_type="webhook", url=secret_url)
        alert = SimpleNamespace(
            id=1,
            name="rule",
            timespan_minutes=1,
            quantity=1,
            uptime=False,
            environments=[],
            alertrecipient_set=SimpleNamespace(all=lambda: [recipient]),
        )
        with patch(
            "apps.assistant.tools.mcp_data.get_alerts",
            new_callable=AsyncMock,
            return_value=[alert],
        ):
            result = async_to_sync(execute_tool)(1, "org", "list_alerts", {})
        self.assertNotIn("SECRETVALUE", result)
        self.assertIn("WEBHOOK_URL", result)
        text = _redact({"message": f"posting to {secret_url} failed"}, scrub_pii=False)
        self.assertNotIn("SECRETVALUE", text["message"])

    def test_unknown_project_never_expands_telemetry_query(self):
        for name, target in [
            ("list_transaction_groups", "get_transaction_groups"),
            ("detect_n_plus_one", "get_n_plus_one_patterns"),
            ("list_logs", "get_logs"),
            ("list_issues", "get_issues"),
            ("list_alerts", "get_alerts"),
        ]:
            with (
                self.subTest(name=name),
                patch(
                    "apps.assistant.tools.mcp_data.get_projects",
                    new_callable=AsyncMock,
                    return_value=[],
                ),
                patch(
                    f"apps.assistant.tools.mcp_data.{target}", new_callable=AsyncMock
                ) as query,
            ):
                result = async_to_sync(execute_tool)(
                    1, "org", name, {"project_slug": "missing"}
                )
                self.assertIn("not found", json.loads(result)["error"])
                query.assert_not_awaited()


class AssistantAccessTests(TestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.organization = baker.make(
            "organizations_ext.Organization", slug="assistant-org"
        )
        self.project = baker.make("projects.Project", organization=self.organization)
        self.organization.add_user(self.user)

    def test_token_ceiling_applies_to_each_tool(self):
        token = SimpleNamespace(get_scopes=lambda: ["event:read"])
        request = SimpleNamespace(
            auth=SimpleNamespace(user_id=self.user.id, auth_type="token", data=token)
        )
        tools = async_to_sync(_allowed_tools)(request, self.organization.id)
        self.assertIn("get_issue", tools)
        self.assertNotIn("list_projects", tools)
        self.assertNotIn("list_alerts", tools)
        self.assertNotIn("list_monitors", tools)
        other_org = baker.make("organizations_ext.Organization", slug="unrelated-org")
        self.assertEqual(async_to_sync(_allowed_tools)(request, other_org.id), set())

    def test_saved_conversations_need_every_tool_scope(self):
        thread = AssistantThread.objects.create(
            organization=self.organization, user=self.user, title="Saved"
        )
        qs = type(self.organization).objects.filter(id=self.organization.id)
        kwargs = {"qs": qs, "organization_id_or_slug": self.organization.slug}
        narrow = SimpleNamespace(get_scopes=lambda: ["event:read"])
        token = SimpleNamespace(
            auth=SimpleNamespace(user_id=self.user.id, auth_type="token", data=narrow)
        )
        session = SimpleNamespace(
            auth=SimpleNamespace(user_id=self.user.id, auth_type="session")
        )
        for view, extra in [(list_threads, {}), (get_thread, {"thread_id": thread.id})]:
            with self.subTest(view=view.__name__):
                with self.assertRaises(HttpError) as ctx:
                    async_to_sync(view.__wrapped__)(token, **kwargs, **extra)
                self.assertEqual(ctx.exception.status_code, 403)
                async_to_sync(view.__wrapped__)(session, **kwargs, **extra)

    def test_issue_short_id_and_cross_org_numeric_id(self):
        issue = make_issue(project=self.project, title="Example", short_id=42)
        other_org = baker.make("organizations_ext.Organization", slug="other-org")
        other_project = baker.make("projects.Project", organization=other_org)
        other_project.organization.add_user(self.user)
        other_issue = make_issue(project=other_project, title="Other")
        with patch(
            "apps.assistant.tools.mcp_data.get_latest_event",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = json.loads(
                async_to_sync(execute_tool)(
                    self.user.id,
                    self.organization.slug,
                    "get_issue",
                    {"issue_id": issue.short_id_display},
                )
            )
            self.assertEqual(result["issue"]["id"], str(issue.id))
            result = json.loads(
                async_to_sync(execute_tool)(
                    self.user.id,
                    self.organization.slug,
                    "get_issue",
                    {"issue_id": str(other_issue.id)},
                )
            )
            self.assertIn("not found", result["error"])

    def test_latest_event_is_returned_for_current_org(self):
        issue = make_issue(project=self.project, short_id=42)
        event = baker.make(
            "issue_events.IssueEvent",
            issue=issue,
            organization=self.organization,
            data={},
            tags={"password": "secret-value"},
        )
        result = json.loads(
            async_to_sync(execute_tool)(
                self.user.id,
                self.organization.slug,
                "get_issue",
                {"issue_id": str(issue.id)},
                scrub_pii=False,
            )
        )
        self.assertEqual(result["latestEvent"]["id"], event.id.hex)
        self.assertNotIn("secret-value", json.dumps(result))

    def test_turn_is_atomic_when_message_write_fails(self):
        thread = AssistantThread(
            organization=self.organization, user=self.user, title="New"
        )
        with (
            patch(
                "apps.assistant.api.AssistantMessage.objects.create",
                side_effect=RuntimeError("failure"),
            ),
            self.assertRaises(RuntimeError),
        ):
            async_to_sync(_save_turn)(
                thread, ChatMessage(role="user", content="hello"), "reply", "session"
            )
        self.assertFalse(AssistantThread.objects.exists())

    def test_fragmented_tool_calls_reset_and_preserve_permissions(self):
        tool_deltas = [
            {"content": "Checking"},
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_",
                        "function": {"name": "list_", "arguments": "{"},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "1",
                        "function": {"name": "monitors", "arguments": "}"},
                    }
                ]
            },
        ]
        rounds = [
            _upstream(tool_deltas, "tool_calls"),
            _upstream([{"content": "Hé"}, {"content": "llo"}], "stop"),
        ]
        token = SimpleNamespace(get_scopes=lambda: ["event:read"])
        request = SimpleNamespace(
            auth=SimpleNamespace(user_id=self.user.id, auth_type="token", data=token)
        )
        payload = ChatRequest(messages=[ChatMessage(role="user", content="hello")])
        bodies, events = self._chat(request, payload, rounds)
        self.assertTrue(all(body["stream"] for body in bodies))
        self.assertNotIn(
            "list_monitors", [tool["function"]["name"] for tool in bodies[0]["tools"]]
        )
        self.assertIn("not permitted", bodies[1]["messages"][-1]["content"])
        self.assertEqual(bodies[1]["messages"][-1]["tool_call_id"], "call_1")
        self.assertEqual(
            [name for name, _ in events],
            ["status", "text", "reset", "status", "status", "text", "text", "done"],
        )
        self.assertEqual(events[-1][1]["reply"], "Héllo")
        self.assertEqual(
            list(AssistantMessage.objects.values_list("content", flat=True)),
            ["hello", "Héllo"],
        )

    def _chat(self, request, payload, rounds, status=200, close_after=None):
        bodies = []

        class Content:
            def __init__(self, data):
                self.data = data

            async def iter_any(self):
                for byte in self.data:
                    yield bytes([byte])

        class Response:
            def __init__(self, data):
                self.status = status
                self.content = Content(data)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class Session:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def post(self, *args, **kwargs):
                bodies.append(kwargs["json"].copy())
                bodies[-1]["messages"] = list(kwargs["json"]["messages"])
                return Response(rounds[len(bodies) - 1])

        async def consume():
            response = await assistant_chat.__wrapped__(
                request,
                qs=type(self.organization).objects.filter(id=self.organization.id),
                organization_id_or_slug=self.organization.slug,
                payload=payload,
            )
            frames = []
            async for chunk in response.streaming_content:
                frames.append(chunk.decode())
                if close_after is not None and len(frames) == close_after:
                    await response._iterator.aclose()
                    break
            return [
                (frame.split("\n", 1)[0][7:], json.loads(frame.split("\ndata: ", 1)[1]))
                for frame in frames
            ]

        with (
            patch.dict(os.environ, {"OPENCODE_API_KEY": "test"}),
            patch("apps.assistant.api.aiohttp.ClientSession", Session),
        ):
            events = async_to_sync(consume)()
        return bodies, events

    def test_failed_inference_does_not_persist_new_or_existing_turn(self):
        request = SimpleNamespace(
            auth=SimpleNamespace(user_id=self.user.id, auth_type="session")
        )
        existing = AssistantThread.objects.create(
            organization=self.organization, user=self.user, title="Existing"
        )
        for thread_id in (None, existing.id):
            payload = ChatRequest(
                messages=[ChatMessage(role="user", content="hello")], threadId=thread_id
            )
            for upstream, status in [
                (b"secret", 500),
                (
                    _upstream([{"content": "partial"}], "stop").replace(
                        b"data: [DONE]\n\n", b""
                    ),
                    200,
                ),
                (_upstream([{"content": "partial"}], "length"), 200),
            ]:
                with self.subTest(
                    thread_id=thread_id, status=status, upstream=upstream
                ):
                    _, events = self._chat(request, payload, [upstream], status)
                    self.assertEqual(events[-1][0], "error")
                    self.assertNotIn("secret", json.dumps(events))
        self.assertEqual(AssistantThread.objects.count(), 1)
        self.assertFalse(AssistantMessage.objects.exists())

    def test_authorized_tool_receives_fragmented_arguments(self):
        request = SimpleNamespace(
            auth=SimpleNamespace(user_id=self.user.id, auth_type="session")
        )
        payload = ChatRequest(messages=[ChatMessage(role="user", content="hello")])
        rounds = [
            _upstream(
                [
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "id",
                                "function": {
                                    "name": "list_issues",
                                    "arguments": '{"project":',
                                },
                            }
                        ]
                    },
                    {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '"example"}'}}
                        ]
                    },
                ],
                "tool_calls",
            ),
            _upstream([{"content": "Done"}], "stop"),
        ]
        with patch(
            "apps.assistant.api.execute_tool", new_callable=AsyncMock, return_value="[]"
        ) as execute:
            _, events = self._chat(request, payload, rounds)
        execute.assert_awaited_once_with(
            self.user.id,
            self.organization.slug,
            "list_issues",
            {"project": "example"},
            scrub_pii=True,
        )
        self.assertEqual(events[-1][1]["toolsUsed"], ["list_issues"])

    def test_tool_failure_emits_error_without_persistence(self):
        request = SimpleNamespace(
            auth=SimpleNamespace(user_id=self.user.id, auth_type="session")
        )
        payload = ChatRequest(messages=[ChatMessage(role="user", content="hello")])
        call = _upstream(
            [
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "id",
                            "function": {"name": "list_issues", "arguments": "{}"},
                        }
                    ]
                },
            ],
            "tool_calls",
        )
        with patch(
            "apps.assistant.api.execute_tool",
            new_callable=AsyncMock,
            side_effect=RuntimeError("secret"),
        ):
            _, events = self._chat(request, payload, [call])
        self.assertEqual(events[-1][0], "error")
        self.assertNotIn("secret", json.dumps(events))
        self.assertFalse(AssistantThread.objects.exists())
        self.assertFalse(AssistantMessage.objects.exists())

    def test_multiple_tool_rounds_and_disconnect(self):
        request = SimpleNamespace(
            auth=SimpleNamespace(user_id=self.user.id, auth_type="session")
        )
        payload = ChatRequest(messages=[ChatMessage(role="user", content="hello")])
        call = _upstream(
            [
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "id",
                            "function": {"name": "list_issues", "arguments": "{}"},
                        }
                    ]
                },
            ],
            "tool_calls",
        )
        with patch(
            "apps.assistant.api.execute_tool", new_callable=AsyncMock, return_value="[]"
        ) as execute:
            bodies, events = self._chat(
                request, payload, [call, call, _upstream([{"content": "Done"}], "stop")]
            )
        self.assertEqual(execute.await_count, 2)
        self.assertEqual(len(bodies), 3)
        self.assertEqual(events[-1][0], "done")
        AssistantMessage.objects.all().delete()
        AssistantThread.objects.all().delete()
        _, events = self._chat(
            request,
            payload,
            [_upstream([{"content": "partial"}], "stop")],
            close_after=2,
        )
        self.assertEqual(events[-1], ("text", {"text": "partial"}))
        self.assertFalse(AssistantThread.objects.exists())
        self.assertFalse(AssistantMessage.objects.exists())


def _upstream(deltas, finish_reason):
    events = [
        {"model": "test-model", "choices": [{"delta": delta, "finish_reason": None}]}
        for delta in deltas
    ]
    events.append({"choices": [{"delta": {}, "finish_reason": finish_reason}]})
    return (
        "".join(
            "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            for event in events
        )
        + "data: [DONE]\n\n"
    ).encode()
