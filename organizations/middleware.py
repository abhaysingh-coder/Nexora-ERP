from urllib.parse import urlencode

from django.http import HttpResponseRedirect
from django.shortcuts import redirect
from django.urls import reverse

from core.organization_context import get_active_organization


BUSINESS_PATH_PREFIXES = (
    '/dashboard/',
    '/accounting/',
    '/banking/',
    '/inventory/',
    '/purchases/',
    '/returns/',
    '/sales/',
    '/taxation/',
)


class OrganizationSelectionMiddleware:
    """Require an authorized organization context before business views run."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path_info
        exempt = path.startswith(('/admin/', '/login/', '/logout/', '/organizations/'))
        if request.user.is_authenticated and not exempt:
            organization = get_active_organization(request)
            if path.startswith(BUSINESS_PATH_PREFIXES) and organization is None:
                choices = request._nexora_available_organizations
                if len(choices) > 1:
                    target = reverse('organizations:select')
                    return HttpResponseRedirect(f'{target}?{urlencode({"next": request.get_full_path()})}')
                return redirect('organizations:access_required')

        return self.get_response(request)
