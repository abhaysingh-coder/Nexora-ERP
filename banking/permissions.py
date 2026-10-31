from core.permissions import has_organization_role


BANKING_OPERATION_ROLES = frozenset({'owner', 'admin', 'accountant', 'finance_manager'})


def can_manage_banking_operations(user, organization):
    """Approved core-finance capability for high-impact banking operations."""
    return has_organization_role(user, organization, BANKING_OPERATION_ROLES)
