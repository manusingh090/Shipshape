from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render


def healthz(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return HttpResponse("ok", content_type="text/plain")


def _wants_json(request):
    return request.path.startswith("/api/")


def bad_request(request, exception=None):
    if _wants_json(request):
        return JsonResponse({"error": "bad_request"}, status=400)
    return render(request, "errors/400.html", status=400)


def forbidden(request, exception=None):
    if _wants_json(request):
        return JsonResponse({"error": "forbidden"}, status=403)
    message = str(exception) if exception and str(exception) else ""
    return render(request, "errors/403.html", {"message": message}, status=403)


def not_found(request, exception=None):
    if _wants_json(request):
        return JsonResponse({"error": "not_found"}, status=404)
    return render(request, "errors/404.html", status=404)


def server_error(request):
    if _wants_json(request):
        return JsonResponse({"error": "server_error"}, status=500)
    return render(request, "errors/500.html", status=500)


def csrf_failure(request, reason=""):
    return render(request, "errors/csrf.html", status=403)
