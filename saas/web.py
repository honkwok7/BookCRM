"""Views for everyone signed in (M5.5b): the announcement banner and its Dismiss button."""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from core.web import is_htmx
from saas.announcements import DISMISSED, for_request


@login_required
def announcements(request):
    where = "portal" if request.GET.get("where") == "portal" else "app"
    rows = for_request(request, where)
    return render(request, "saas/_announcements.html", {"announcements": rows})


@login_required
@require_POST
def dismiss(request, pk):
    dismissed = request.session.get(DISMISSED, [])
    if str(pk) not in dismissed:
        request.session[DISMISSED] = [*dismissed, str(pk)][-50:]
    if is_htmx(request):
        return HttpResponse("")
    return redirect("home")
