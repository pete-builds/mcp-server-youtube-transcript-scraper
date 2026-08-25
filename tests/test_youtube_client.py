

class TestHttpTimeout:
    """A bare requests.Session has no default timeout, and the upstream library
    builds one when handed no http_client. A stalled socket then blocks its
    worker thread forever, and nothing else bounds it: FastMCP's per-tool timeout
    is unset, the web server does not time out in-flight requests, and /health is
    an async route that stays green while every thread is wedged -- so the
    container's restart policy never fires.
    """

    def test_session_applies_a_default_timeout(self) -> None:
        import requests

        from mcp_youtube.clients.youtube import (
            DEFAULT_HTTP_TIMEOUT_SECONDS,
            _timeout_session,
        )

        session = _timeout_session(DEFAULT_HTTP_TIMEOUT_SECONDS)
        captured: dict = {}
        original = requests.Session.request

        def spy(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            raise RuntimeError("stopped before any network call")

        requests.Session.request = spy  # type: ignore[assignment]
        try:
            session.get("http://example.invalid")
        except RuntimeError:
            pass
        finally:
            requests.Session.request = original  # type: ignore[assignment]

        assert captured.get("timeout") == DEFAULT_HTTP_TIMEOUT_SECONDS

    def test_explicit_timeout_is_not_overridden(self) -> None:
        from mcp_youtube.clients.youtube import _timeout_session

        session = _timeout_session(30.0)
        import requests

        captured: dict = {}
        original = requests.Session.request

        def spy(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            raise RuntimeError("stop")

        requests.Session.request = spy  # type: ignore[assignment]
        try:
            session.get("http://example.invalid", timeout=5.0)
        except RuntimeError:
            pass
        finally:
            requests.Session.request = original  # type: ignore[assignment]

        assert captured.get("timeout") == 5.0, "an explicit per-call timeout must win"
