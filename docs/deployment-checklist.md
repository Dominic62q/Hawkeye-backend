# Hawkeye deployment checklist

Settings that default to development-friendly values and must be set for production. Most are environment variables; none need code changes. Tick each off per environment.

## Required

- [ ] `DEBUG` is false (unset or `false`).
- [ ] `SECRET_KEY` is a unique random secret, not the `change_me` placeholder. Generate one with `python -c 'import secrets; print(secrets.token_urlsafe(50))'`. Startup warns if it is still the default (`glitchtip/settings.py:114`).
- [ ] `ALLOWED_HOSTS` lists every real hostname that reaches the backend, comma-separated (for example `hawkeye.example.com,api.example.com`). The default `*` accepts any `Host` header and enables host-header poisoning of reset and invite links. Startup warns on the wildcard (`glitchtip/settings.py:123`). A missing hostname returns 400, and the rejection is not logged (the `DisallowedHost` logger is silenced), so test each real URL after setting it.
- [ ] `GLITCHTIP_URL` is the full public URL with `https://`. It is used in emails, DSNs and permalinks, and it turns on secure cookies when it is `https` (`glitchtip/settings.py:876`).
- [ ] `CSRF_TRUSTED_ORIGINS` includes the UI origin if the UI is served from a different origin than the API.
- [ ] `TRUSTED_PROXIES` is set when running behind a proxy or load balancer, so client IPs and rate limits are correct. It defaults to private networks (`glitchtip/proxy_headers.py`), and the app logs the proxies it sees once, to help you choose a value.

## Operator admin (`/admin/`)

- [ ] Decide whether the admin is needed in production. If not, set `ENABLE_ADMIN=false`.
- [ ] If it is needed, restrict `/admin/` to your own IP at the reverse proxy.
- [ ] MFA is enabled on every superuser account (account settings, TOTP or WebAuthn). Admin login goes through allauth, so MFA applies once enabled.
- [ ] Only the intended superuser(s) exist. Check with `python manage.py shell -c "from apps.users.models import User; print(list(User.objects.filter(is_superuser=True).values_list('email', flat=True)))"`.

## Before launch

- [ ] Billing is off or intentionally on. It turns on only when both `STRIPE_PUBLIC_KEY` and `STRIPE_SECRET_KEY` are set (`glitchtip/settings.py:817`), so leave them unset to keep it off.
- [ ] Email delivery works end to end (sign-up, password reset, invite) with the production sender.
- [ ] Backups of the database are configured and a restore has been tried.
- [ ] Startup logs show no `RuntimeWarning` about unsafe defaults.
