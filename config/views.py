from __future__ import annotations

from django.http import HttpRequest, JsonResponse


def healthz(_request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})
