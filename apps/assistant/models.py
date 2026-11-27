import uuid

from django.conf import settings
from django.db import models


class AssistantThread(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations_ext.Organization",
        on_delete=models.CASCADE,
        related_name="assistant_threads",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="assistant_threads",
    )
    title = models.CharField(max_length=120)
    project_name = models.CharField(max_length=255, default="All projects")
    session_id = models.CharField(max_length=64, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ["-updated"]


class AssistantMessage(models.Model):
    thread = models.ForeignKey(
        AssistantThread,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    role = models.CharField(max_length=16)
    content = models.TextField()
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created", "id"]


class OrganizationAssistantSettings(models.Model):
    organization = models.OneToOneField(
        "organizations_ext.Organization",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="assistant_settings",
    )
    enabled = models.BooleanField(default=True)
    scrub_pii = models.BooleanField(default=True)
    updated = models.DateTimeField(auto_now=True)
