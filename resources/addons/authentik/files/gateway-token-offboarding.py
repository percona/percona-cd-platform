# Runs inside `ak shell -c` in the Authentik image (Django ORM, no API token).
# Finds refresh tokens of one OAuth2 provider whose user has not signed in
# for OFFBOARDING_MAX_LOGIN_AGE_DAYS. "report" only logs them, "revoke"
# deletes them. A deleted token fails the next refresh with invalid_grant,
# so the client has to sign in through SSO again.
import json
import os
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Q
from django.utils.timezone import now

from authentik.providers.oauth2.models import OAuth2Provider, RefreshToken

mode = os.environ["OFFBOARDING_MODE"]
if mode not in ("report", "revoke"):
    raise SystemExit(f"OFFBOARDING_MODE must be report or revoke, got {mode!r}")
provider_name = os.environ["OFFBOARDING_PROVIDER"]
max_age_days = int(os.environ["OFFBOARDING_MAX_LOGIN_AGE_DAYS"])
max_users = int(os.environ["OFFBOARDING_MAX_USERS"])
include_service_accounts = os.environ.get("OFFBOARDING_INCLUDE_SERVICE_ACCOUNTS") == "true"

# DoesNotExist ends the run with an error, so a renamed provider is visible.
provider = OAuth2Provider.objects.get(name=provider_name)
cutoff = now() - timedelta(days=max_age_days)
stale = RefreshToken.objects.filter(provider=provider).filter(
    Q(user__last_login__lt=cutoff) | Q(user__last_login__isnull=True)
)
if not include_service_accounts:
    stale = stale.exclude(user__type__in=["service_account", "internal_service_account"])

per_user = list(stale.values("user__username").annotate(tokens=Count("pk")).order_by("user__username"))
for row in per_user:
    print(json.dumps({"event": "gateway_token_offboarding_user", "username": row["user__username"], "tokens": row["tokens"]}))

summary = {
    "event": "gateway_token_offboarding",
    "mode": mode,
    "provider": provider_name,
    "cutoff": cutoff.isoformat(),
    "users": len(per_user),
    "tokens": sum(row["tokens"] for row in per_user),
    "deleted_rows": 0,
}
if len(per_user) > max_users:
    summary["error"] = f"{len(per_user)} users exceed OFFBOARDING_MAX_USERS={max_users}, nothing deleted"
    print(json.dumps(summary))
    raise SystemExit(1)
if mode == "revoke" and per_user:
    with transaction.atomic():
        summary["deleted_rows"] = stale.delete()[0]
print(json.dumps(summary))
