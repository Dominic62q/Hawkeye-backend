from django.contrib import admin

from .models import Issue


class _IssueLabel:
    """Stands in for an Issue in the admin page chrome. Django prints ``str(issue)`` there, which is the title."""

    def __init__(self, issue: Issue):
        self.pk = issue.pk
        self._label = f"Issue {issue.pk}"

    def __str__(self):
        return self._label


@admin.register(Issue)
class IssueAdmin(admin.ModelAdmin):
    """View-only. Title, culprit and metadata can contain customer data, so they are not shown here."""

    list_display = ("id", "first_seen")
    fields = ("id", "project", "type", "short_id", "first_seen", "is_public")
    readonly_fields = fields
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def render_change_form(
        self, request, context, add=False, change=False, form_url="", obj=None
    ):
        if obj is not None:
            label = _IssueLabel(obj)
            context["original"] = label
            context["subtitle"] = str(label)
        return super().render_change_form(
            request, context, add=add, change=change, form_url=form_url, obj=obj
        )
