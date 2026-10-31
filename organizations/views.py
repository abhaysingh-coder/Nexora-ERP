from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods

from core.organization_context import (
    ACTIVE_ORGANIZATION_SESSION_KEY,
    available_organizations,
    get_active_organization,
)

from .forms import OrganizationSelectionForm


@login_required
@require_http_methods(['GET', 'POST'])
def select_organization(request):
    choices = list(available_organizations(request.user))
    next_url = request.POST.get('next') or request.GET.get('next', '')

    if request.method == 'POST':
        form = OrganizationSelectionForm(request.POST, user=request.user)
        if not form.is_valid():
            return HttpResponseForbidden('Choose an active organization you are authorized to access.')
        organization = form.cleaned_data['organization']
        request.session[ACTIVE_ORGANIZATION_SESSION_KEY] = organization.pk
        request._nexora_active_organization = organization
        request.organization = organization
        messages.success(request, f'Active organization changed to {organization.name}.')
        if next_url and url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return redirect(next_url)
        return redirect('dashboard')

    form = OrganizationSelectionForm(user=request.user)
    active = get_active_organization(request)
    return render(request, 'organizations/select_organization.html', {
        'form': form,
        'active_organization': active,
        'next_url': next_url if next_url and next_url.startswith('/') and not next_url.startswith('//') else '',
        'active_page': 'organization',
    })


@login_required
def access_required(request):
    return render(request, 'organizations/access_required.html', {
        'organization_count': len(available_organizations(request.user)),
        'active_page': 'organization',
    }, status=403)
