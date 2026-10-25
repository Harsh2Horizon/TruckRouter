"""Independent optimizer endpoints with shared validation and error handling."""

import json
import logging

from django.core.exceptions import RequestDataTooBig
from django.db import DatabaseError
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from pydantic import ValidationError

from . import dijkstra, greedy
from .contracts import OptimizationError, OptimizationRequest
from .service import execute

logger = logging.getLogger(__name__)


def error(code, message, status):
    return JsonResponse({"error": {"code": code, "message": message}}, status=status)


def respond(request, algorithm, optimizer):
    if request.method != "POST":
        response = error("method_not_allowed", "Use POST with an application/json body", 405)
        response["Allow"] = "POST"
        return response
    try:
        if request.content_type != "application/json":
            return error("invalid_content_type", "Content-Type must be application/json", 415)
        query = OptimizationRequest.model_validate(json.loads(request.body))
        return JsonResponse(execute(query, algorithm, optimizer))
    except (ValidationError, ValueError, TypeError, RequestDataTooBig) as exc:
        return error("invalid_request", str(exc), 400)
    except OptimizationError as exc:
        return error(exc.code, str(exc), exc.status)
    except DatabaseError:
        logger.exception("Fuel station corridor query failed")
        return error(
            "station_data_unavailable", "The station spatial index could not be queried", 503
        )


@csrf_exempt
def greedy_view(request):
    return respond(request, "greedy", greedy.optimize)


@csrf_exempt
def dijkstra_view(request):
    return respond(request, "dijkstra", dijkstra.optimize)
