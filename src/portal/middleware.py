CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "img-src 'self' data:",
    "style-src 'self'",
    "script-src 'self'",
    "font-src 'self'",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "form-action 'self'",
    "base-uri 'none'",
    "object-src 'none'",
])


class SecurityHeadersMiddleware:
    """Headers Django does not set on its own.

    The CSP allows nothing inline and nothing from another origin. That is
    also how we know the portal works offline: a page that tried to load a
    font or script from a CDN would be blocked here first.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"
        )
        return response
