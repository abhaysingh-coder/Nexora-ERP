"""Apply explicit HTTP method allowlists to every non-admin URL pattern."""

from functools import wraps

from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponseNotAllowed
from django.urls import URLPattern, URLResolver


def enforce_route_methods(urlpatterns, method_policies, *, prefix=''):
    """Wrap application routes with a fail-closed, route-name method policy.

    Every non-admin route must appear exactly once in ``method_policies``.
    Admin methods remain controlled by Django's admin views.
    """
    seen = set()
    for pattern in urlpatterns:
        route = f'{prefix}{pattern.pattern}'
        if route.startswith('admin/'):
            continue
        if isinstance(pattern, URLResolver):
            seen.update(enforce_route_methods(
                pattern.url_patterns,
                method_policies,
                prefix=route,
            ))
            continue
        if not isinstance(pattern, URLPattern):
            continue
        if pattern.name is None:
            raise ImproperlyConfigured(f'Application URL has no name: {route}')
        if pattern.name not in method_policies:
            raise ImproperlyConfigured(f'Application URL has no method policy: {pattern.name} ({route})')
        if pattern.name in seen:
            raise ImproperlyConfigured(f'Duplicate route method policy name: {pattern.name}')
        methods = tuple(method_policies[pattern.name])
        if not methods or any(method not in {'GET', 'HEAD', 'POST'} for method in methods):
            raise ImproperlyConfigured(f'Invalid method allowlist for {pattern.name}: {methods}')
        callback = pattern.callback

        @wraps(callback)
        def method_checked(request, *args, __callback=callback, __methods=methods, **kwargs):
            if request.method not in __methods:
                return HttpResponseNotAllowed(__methods)
            return __callback(request, *args, **kwargs)

        method_checked.nexora_allowed_methods = methods
        pattern.callback = method_checked
        seen.add(pattern.name)
    return seen
