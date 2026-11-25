from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

from .models import Membership


def club_member_required(owner_only=False, allow_inactive=False):
    """Dashboard access for people who work at the current club (owners and staff).
    `owner_only` limits a page to owners. Unless `allow_inactive`, a club whose subscription
    expired is sent to its subscription page."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            role = getattr(request, "club_role", None)
            if role is None:
                return redirect_to_login(request.get_full_path())
            if owner_only and role != Membership.Role.OWNER:
                raise PermissionDenied("Only the club owner can open this page.")
            if not allow_inactive and not request.club.is_operational and not request.user.is_superuser:
                return redirect("dashboard:subscription")
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


staff_required = club_member_required()
owner_required = club_member_required(owner_only=True)
