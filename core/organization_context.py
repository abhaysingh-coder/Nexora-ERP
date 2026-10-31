from organizations.models import Branch, Organization


ACTIVE_ORGANIZATION_SESSION_KEY = 'active_organization_id'


def available_organizations(user):
    """Return only active organizations the user may select for this session."""
    if not user.is_authenticated:
        return Organization.objects.none()

    if user.is_superuser:
        organizations = Organization.objects.filter(is_active=True)
    else:
        organizations = Organization.objects.filter(
            is_active=True,
            memberships__user=user,
            memberships__is_active=True,
        ).distinct()

    if user.branch_id:
        try:
            branch = user.branch
        except Branch.DoesNotExist:
            return Organization.objects.none()
        if not branch.is_active:
            return Organization.objects.none()
        organizations = organizations.filter(pk=branch.organization_id)

    return organizations.order_by('name', 'pk')


def get_active_organization(request):
    """Resolve the session's authorized organization without choosing ambiguously."""
    if hasattr(request, '_nexora_active_organization'):
        return request._nexora_active_organization

    organizations = list(available_organizations(request.user))
    request._nexora_available_organizations = organizations
    selected_id = request.session.get(ACTIVE_ORGANIZATION_SESSION_KEY)
    selected = next((org for org in organizations if str(org.pk) == str(selected_id)), None)

    if selected is None and selected_id is not None:
        request.session.pop(ACTIVE_ORGANIZATION_SESSION_KEY, None)

    if selected is None and len(organizations) == 1:
        selected = organizations[0]

    request._nexora_active_organization = selected
    request.organization = selected
    return selected
