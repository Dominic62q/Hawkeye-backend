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
- [ ] If it is needed, restrict `/admin/` to your own IP at the reverse proxy. Do the same for the operator console: `/operator` (the UI) and `/api/0/operator/` (its API). The API already refuses anyone who is not a logged-in superuser, so the allowlist is an extra layer.
- [ ] MFA is enabled on every superuser account (account settings, TOTP or WebAuthn). Admin login goes through allauth, so MFA applies once enabled.
- [ ] Only the intended superuser(s) exist. Check with `python manage.py shell -c "from apps.users.models import User; print(list(User.objects.filter(is_superuser=True).values_list('email', flat=True)))"`.

## Before launch

- [ ] Billing is off or intentionally on. It turns on only when both `STRIPE_PUBLIC_KEY` and `STRIPE_SECRET_KEY` are set (`glitchtip/settings.py:817`), so leave them unset to keep it off.
- [ ] Email delivery works end to end (sign-up, password reset, invite) with the production sender. Until then, accounts are not email-verified and the password reset flow is hidden, so a locked-out user needs a manual reset.
  - Mailtrap, through its API: `EMAIL_BACKEND=anymail.backends.mailtrap.EmailBackend` and `MAILTRAP_API_TOKEN`. Add `MAILTRAP_SANDBOX_ID` to send to a Mailtrap test inbox instead of real recipients. Set `DEFAULT_FROM_EMAIL` to a sender Mailtrap allows (its demo sender, or an address on a verified domain). Any other provider's SMTP details also work through `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` and `EMAIL_USE_TLS`.
- [ ] Decide who may sign up. `ENABLE_USER_REGISTRATION` defaults to on, so anyone who finds the URL can create an account. Set it to `false` for invite-only; the first user on an empty instance is still allowed.
- [ ] Backups of the database are configured and a restore has been tried.
- [ ] Startup logs show no `RuntimeWarning` about unsafe defaults.

## Later: social sign-in

Not used at launch: sign-in is email and password only. With no social application added, no social button shows on `/login` or `/signup`, so there is nothing to switch off. When you want it:

- [ ] Create an OAuth app at the provider (Google Cloud Console credentials, GitHub OAuth Apps, Microsoft Entra app registrations, GitLab Applications). Google also needs its consent screen published, or only listed test users can sign in.
- [ ] Register the callback URL exactly, with the trailing slash: `https://<your-domain>/accounts/<provider>/login/callback/` (for example `.../accounts/google/login/callback/`). Fix the production domain first.
- [ ] In `/admin/`, add a Social application with the provider, a display name and the client id and secret. The row lives in the database, so each environment needs its own app and keys. Enter the secret yourself and never paste it into chat or commit it.
- [ ] Provider settings that need environment variables: `SOCIALACCOUNT_PROVIDERS_microsoft_TENANT`, `SOCIALACCOUNT_PROVIDERS_gitlab_GITLAB_URL` for self-hosted GitLab, and the Gitea and Nextcloud equivalents.
- [ ] The proxy forwards `/_allauth`, `/accounts`, `/api`, `/oauth`, `/admin` and `/static` to the backend, on the same host as the UI.
- [ ] Decide whether social sign-up is open: `ENABLE_SOCIAL_APPS_USER_REGISTRATION` follows `ENABLE_USER_REGISTRATION` unless set. Existing users can sign in either way.
- [ ] Test with real accounts: a new email, an email that already has a password account (no automatic linking is configured, so expect an error), and a superuser with MFA on. The hawkeye-ui has no screen yet for organization-managed SSO or for linking and unlinking providers on an account.
