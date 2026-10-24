import json

from django.core.exceptions import RequestDataTooBig
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from pydantic import ValidationError

from .models import GeocodingRun
from .parsing import parse_address
from .services import resolve


def read_address(request):
    if request.content_type != "application/json":
        raise ValueError("Content-Type must be application/json")
    return parse_address(json.loads(request.body))


# Local prototype API; add authentication/throttling before an external deployment.
@csrf_exempt
@require_POST
def parse_view(request):
    try:
        return JsonResponse(read_address(request).model_dump(mode="json"))
    except (ValidationError, ValueError, TypeError, RequestDataTooBig) as exc:
        return JsonResponse({"error": str(exc)}, status=400)


@csrf_exempt
@require_POST
def geocode_view(request):
    try:
        query = read_address(request)
    except (ValidationError, ValueError, TypeError, RequestDataTooBig) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    run = resolve(query)
    return JsonResponse({"run_id": str(run.pk), "query": run.query, **run.result})


@require_GET
def run_view(request, run_id):
    run = get_object_or_404(GeocodingRun, pk=run_id)
    return JsonResponse({"run_id": str(run.pk), "query": run.query, **run.result})
