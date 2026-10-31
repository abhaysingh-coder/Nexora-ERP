from core.organization_context import available_organizations, get_active_organization


def organization_context(request):
    if not request.user.is_authenticated:
        return {}

    organization = get_active_organization(request)
    organizations = getattr(request, '_nexora_available_organizations', None)
    if organizations is None:
        organizations = list(available_organizations(request.user))
    return {
        'organization': organization,
        'available_organizations': organizations,
        'organization_selection_required': len(organizations) > 1 and organization is None,
    }
