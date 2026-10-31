"""Small shared authorization predicates that preserve module role policies."""


def has_organization_role(user, organization, roles):
    """Return whether a user has any active membership role in an organization.

    Role names and their permitted actions remain owned by each module's
    existing policy. Superuser bypass behavior is retained where the caller
    already used it; this helper does not establish branch or object access.
    """
    if not user.is_authenticated or not user.is_active or organization is None:
        return False
    if user.is_superuser:
        return True
    return user.organizations_memberships.filter(
        organization=organization,
        is_active=True,
        role__in=roles,
    ).exists()
