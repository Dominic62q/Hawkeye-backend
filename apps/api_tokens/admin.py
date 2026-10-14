from django.contrib import admin

from bitfield import BitField
from bitfield.forms import BitFieldCheckboxSelectMultiple

from .models import APIToken


@admin.register(APIToken)
class APITokenAdmin(admin.ModelAdmin):
    raw_id_fields = ("user",)
    # The token is a live credential; the admin only ever shows the last four characters.
    list_display = ("label", "user", "token_hint", "created")
    readonly_fields = ("token_hint", "created")
    fields = ("user", "label", "scopes", "token_hint", "created")
    formfield_overrides = {
        BitField: {"widget": BitFieldCheckboxSelectMultiple},
    }
