"""Inviting people to an organization.

    invite ─► email with a one-time link ─► accept (signed in, same verified email) ─► member
                                          └► decline

Only the token's hash is stored. Resending issues a new token, so an old link stops working.
Accepting requires the invitee to be signed in with the invited address, already verified:
holding the link alone is not enough to join.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone

from nexa_common.errors import Conflict, ErrorCode, NexaError, NotFound, PermissionDenied
from nexa_core import request_meta
from nexa_core.access import rules
from nexa_core.access.principal import OrganizationContext
from nexa_core.audit.actions import AuditAction
from nexa_core.audit.service import record
from nexa_core.identity import emails
from nexa_core.identity.managers import normalize_email
from nexa_core.identity.models import User
from nexa_core.organizations.models import Invitation, OrganizationMembership
from nexa_core.organizations.services import get_role, organization_settings
from nexa_core.security import ratelimit
from nexa_core.security.one_time import hash_secret, new_secret

MAX_SENDS = 5


@dataclass(frozen=True)
class InvitationPreview:
    organization_name: str
    inviter_name: str
    role_name: str
    email: str
    expires_at: datetime


def _expire_stale(organization_id: UUID, email: str) -> None:
    Invitation.objects.filter(
        organization_id=organization_id,
        email=email,
        status=Invitation.Status.PENDING,
        expires_at__lte=timezone.now(),
    ).update(status=Invitation.Status.EXPIRED)


def _send(invitation: Invitation, raw_token: str, inviter: User) -> None:
    emails.invitation_email(
        invitation.email,
        organization=invitation.organization.name,
        inviter=inviter.full_name or inviter.email,
        role=invitation.role.name,
        token=raw_token,
    )


def _get_pending(context: OrganizationContext, invitation_id: UUID) -> Invitation:
    invitation = (
        Invitation.objects.select_related("organization", "role")
        .filter(id=invitation_id, organization=context.organization)
        .first()
    )
    if invitation is None:
        raise NotFound
    if invitation.status != Invitation.Status.PENDING:
        raise Conflict("This invitation is no longer pending.")
    return invitation


@transaction.atomic
def create_invitation(
    context: OrganizationContext, *, email: str, role_id: UUID, inviter: User
) -> Invitation:
    if context.organization.is_personal:
        raise Conflict("Personal workspaces can't have other members. Create an organization.")
    ratelimit.consume(ratelimit.INVITATIONS_PER_ORGANIZATION, str(context.organization.id))
    email = normalize_email(email)
    role = get_role(context, role_id)
    rules.ensure_can_grant(context.principal, role)

    if OrganizationMembership.objects.filter(
        organization=context.organization, user__email=email
    ).exists():
        raise Conflict("This person is already a member.")
    _expire_stale(context.organization.id, email)

    raw = new_secret()
    days = organization_settings(context.organization).invitation_expiry_days
    try:
        with transaction.atomic():
            invitation = Invitation.objects.create(
                organization=context.organization,
                email=email,
                role=role,
                invited_by=inviter,
                token_hash=hash_secret(raw),
                expires_at=timezone.now() + timedelta(days=days),
            )
    except IntegrityError as exc:  # the one-pending-per-email constraint
        raise Conflict("A pending invitation already exists for this email.") from exc

    record(
        AuditAction.MEMBER_INVITED,
        actor_id=inviter.id,
        organization_id=context.organization.id,
        resource_type="invitation",
        resource_id=invitation.id,
        metadata={"role": role.key},
    )
    _send(invitation, raw, inviter)
    return invitation


@transaction.atomic
def resend_invitation(
    context: OrganizationContext, invitation_id: UUID, inviter: User
) -> Invitation:
    invitation = _get_pending(context, invitation_id)
    if invitation.sent_count >= MAX_SENDS:
        raise Conflict("This invitation has been sent too many times. Revoke it and invite again.")
    raw = new_secret()
    days = organization_settings(context.organization).invitation_expiry_days
    invitation.token_hash = hash_secret(raw)  # the previous link stops working
    invitation.expires_at = timezone.now() + timedelta(days=days)
    invitation.sent_count += 1
    invitation.last_sent_at = timezone.now()
    invitation.save()
    record(
        AuditAction.INVITATION_RESENT,
        actor_id=inviter.id,
        organization_id=context.organization.id,
        resource_type="invitation",
        resource_id=invitation.id,
    )
    _send(invitation, raw, inviter)
    return invitation


@transaction.atomic
def revoke_invitation(context: OrganizationContext, invitation_id: UUID) -> None:
    invitation = _get_pending(context, invitation_id)
    invitation.status = Invitation.Status.REVOKED
    invitation.responded_at = timezone.now()
    invitation.save(update_fields=["status", "responded_at"])
    record(
        AuditAction.INVITATION_REVOKED,
        actor_id=context.principal.user_id,
        organization_id=context.organization.id,
        resource_type="invitation",
        resource_id=invitation.id,
    )


def _open_invitation(raw_token: str, *, lock: bool = False) -> Invitation:
    ratelimit.consume(ratelimit.TOKEN_REDEEM_PER_IP, request_meta.current().ip_address or "unknown")
    query = Invitation.objects.select_related("organization", "role", "invited_by")
    if lock:
        query = query.select_for_update(of=("self",))
    invitation = query.filter(token_hash=hash_secret(raw_token)).first()
    if (
        invitation is None
        or invitation.status != Invitation.Status.PENDING
        or invitation.organization.deleted_at is not None
    ):
        raise NexaError(code=ErrorCode.TOKEN_INVALID, status=400)
    if invitation.is_expired:
        raise NexaError("This invitation has expired.", code=ErrorCode.TOKEN_EXPIRED, status=400)
    return invitation


def preview_invitation(raw_token: str) -> InvitationPreview:
    invitation = _open_invitation(raw_token)
    inviter = invitation.invited_by
    return InvitationPreview(
        organization_name=invitation.organization.name,
        inviter_name=(inviter.full_name or inviter.email) if inviter else "A NEXA user",
        role_name=invitation.role.name,
        email=invitation.email,
        expires_at=invitation.expires_at,
    )


@transaction.atomic
def accept_invitation(raw_token: str, user: User) -> OrganizationMembership:
    invitation = _open_invitation(raw_token, lock=True)
    if user.email != invitation.email or not user.is_verified:
        raise PermissionDenied(
            "This invitation was sent to a different email address. Sign in with that address."
        )
    if OrganizationMembership.objects.filter(
        organization=invitation.organization, user=user
    ).exists():
        raise Conflict("You are already a member of this organization.")

    membership = OrganizationMembership.objects.create(
        organization=invitation.organization,
        user=user,
        role=invitation.role,
        invited_by=invitation.invited_by,
    )
    invitation.status = Invitation.Status.ACCEPTED
    invitation.accepted_by = user
    invitation.responded_at = timezone.now()
    invitation.save(update_fields=["status", "accepted_by", "responded_at"])
    record(
        AuditAction.MEMBER_JOINED,
        actor_id=user.id,
        organization_id=invitation.organization_id,
        resource_type="membership",
        resource_id=membership.id,
        metadata={"invitation_id": str(invitation.id), "role": invitation.role.key},
    )
    return membership


@transaction.atomic
def decline_invitation(raw_token: str) -> None:
    invitation = _open_invitation(raw_token, lock=True)
    invitation.status = Invitation.Status.DECLINED
    invitation.responded_at = timezone.now()
    invitation.save(update_fields=["status", "responded_at"])
    record(
        AuditAction.INVITATION_DECLINED,
        organization_id=invitation.organization_id,
        resource_type="invitation",
        resource_id=invitation.id,
    )
