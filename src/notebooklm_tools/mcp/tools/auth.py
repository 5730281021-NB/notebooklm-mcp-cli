"""Auth tools - Authentication management."""

import os
import time
import urllib.parse

from ._utils import (
    ESSENTIAL_COOKIES,
    ResultDict,
    error_result,
    get_client,
    logged_tool,
    reset_client,
)


def _auth_is_usable() -> tuple[bool, str | None]:
    """Return (usable, reason) by probing with the SAME tokens get_client() uses.

    Why not ``check_auth(live=True)`` or ``AuthHealthChecker``?

    - ``check_auth`` only does a homepage fetch, which redirects to the Google
      login page whenever the fast-rotating cookies (``__Secure-1PSIDTS``,
      ``SIDCC``, …) lag behind — even when the RPC API still accepts the jar.
    - ``AuthHealthChecker`` loads cookies from the multi-profile store as a
      *list* and funnels it through ``_cookies_to_dict()``, which silently
      drops same-named/cross-domain entries (observed: 49 cookies → 28). The
      thinned-out jar fails the API probe even though the real credentials are
      fine.

    ``get_client()`` authenticates from ``load_cached_tokens()`` (the unified
    ``auth.json`` dict, 49 cookies), so the only faithful liveness test is to
    probe with that exact same token set. We do a single lightweight
    ``list_notebooks`` call and treat transport errors as "unknown but usable"
    rather than "expired", so a flaky network never forces a needless re-auth.
    """
    try:
        from notebooklm_tools.services.auth import load_cached_tokens

        cached = load_cached_tokens()
        if not cached or not cached.cookies:
            return False, "no_tokens"

        from notebooklm_tools.core.client import NotebookLMClient

        client = NotebookLMClient(
            cookies=cached.cookies,
            csrf_token=cached.csrf_token or "",
            session_id=cached.session_id or "",
            build_label=cached.build_label or "",
        )
        client.list_notebooks()
        return True, None
    except Exception as e:
        import httpx as _httpx

        if isinstance(e, (_httpx.TimeoutException, _httpx.RequestError)):
            # Transport-level failure (timeout, DNS, connection refused). We
            # genuinely don't know — don't report "expired" and trigger a
            # pointless re-auth on a network blip.
            return False, f"network_error: {type(e).__name__}"
        return False, "expired"


def _headless_reauth_via_login_module() -> ResultDict | None:
    """Attempt a non-interactive token refresh via the `nlm login` module.

    Uses ``run_headless_auth()`` (the same CDP-based login flow that powers
    ``nlm login``) against the saved Chrome profile, so no browser window or
    user interaction is required. The freshly extracted tokens are validated
    live before the client is rebuilt, so we never hand back credentials that
    are already dead.

    Returns:
        A success ``ResultDict`` if headless re-auth refreshed valid tokens,
        otherwise ``None`` to signal the caller to fall back to a manual
        ``nlm login`` hint.
    """
    try:
        from notebooklm_tools.utils.cdp import has_chrome_profile, run_headless_auth
    except Exception:
        return None

    # No saved Chrome login means headless auth physically cannot work —
    # don't spin up Chrome just to fail.
    try:
        if not has_chrome_profile():
            return None
    except Exception:
        return None

    # Snapshot the current on-disk tokens BEFORE running headless auth.
    # run_headless_auth() extracts whatever cookies the Chrome profile holds
    # and writes them to disk immediately — even if the profile's own Google
    # session has expired (validate_cookies only checks cookie *presence*,
    # not liveness). That can clobber still-API-valid cached tokens with dead
    # ones. We keep the old tokens so we can roll back on failure.
    from notebooklm_tools.services.auth import (
        load_cached_tokens,
        save_tokens_to_cache,
    )

    previous_tokens = None
    try:
        previous_tokens = load_cached_tokens()
    except Exception:
        previous_tokens = None

    def _restore_previous() -> None:
        if previous_tokens is not None:
            try:
                save_tokens_to_cache(previous_tokens, silent=True)
            except Exception:
                pass

    try:
        tokens = run_headless_auth()
    except Exception:
        _restore_previous()
        return None

    if not tokens:
        _restore_previous()
        return None

    # Validate the freshly extracted tokens before trusting them. The Chrome
    # profile's saved Google session can itself be expired, in which case
    # headless extraction "succeeds" but yields dead cookies. If so, restore
    # the previous tokens (which may still be accepted by the API) rather than
    # leaving the dead ones that headless auth just wrote.
    usable, _reason = _auth_is_usable()
    if not usable:
        _restore_previous()
        return None

    reset_client()
    get_client()
    return {
        "status": "success",
        "message": (
            "Auth tokens refreshed via the nlm login module (headless Chrome) "
            "and validated."
        ),
    }


@logged_tool()
def refresh_auth(force: bool = False) -> ResultDict:
    """Refresh NotebookLM auth tokens, re-authenticating via `nlm login` if needed.

    Resolution order:
      1. Reload tokens from disk (after an external `nlm login`) and validate
         them live. If valid, the client is rebuilt and we're done.
      2. If the on-disk tokens are missing or expired, automatically run the
         `nlm login` module in headless mode (CDP against the saved Chrome
         profile, no browser window) to mint fresh tokens.
      3. Only if headless re-auth is impossible (no saved Chrome login or the
         saved Google session itself expired) do we ask the user to run
         `nlm login` manually.

    Args:
        force: If True, skip the disk-validity shortcut and re-authenticate via
            the headless login module even when the cached tokens still look
            valid. Useful for proactively rotating soon-to-expire credentials.

    Returns status indicating if tokens were refreshed successfully.
    """
    try:
        # If NOTEBOOKLM_COOKIES is set in the environment (e.g. claude_desktop_config.json),
        # it overrides all disk-based auth. Disk reload won't help — the env var wins on
        # every client re-init. Tell the user exactly what to do instead of lying with "success".
        if os.environ.get("NOTEBOOKLM_COOKIES"):
            return error_result(
                "NOTEBOOKLM_COOKIES is set as an environment variable in your MCP config. "
                "This overrides all other auth sources (auth.json, nlm login, save_auth_tokens). "
                "To fix: update the cookie value in your MCP config file "
                "(e.g. claude_desktop_config.json) and restart, "
                "or remove the NOTEBOOKLM_COOKIES env var and use 'nlm login' instead."
            )

        from notebooklm_tools.services.auth import load_cached_tokens

        cached = load_cached_tokens()

        # Fast path: existing tokens on disk that are still usable (homepage OR
        # API probe passes). Skipped when the caller forces a refresh.
        if cached and not force:
            usable, _reason = _auth_is_usable()
            if usable:
                reset_client()
                get_client()
                return {
                    "status": "success",
                    "message": "Auth tokens reloaded from disk cache and validated.",
                }
            # Tokens are present but dead — fall through to headless re-auth
            # via the nlm login module instead of giving up immediately.

        # Recovery path: drive the nlm login module headlessly to mint fresh
        # tokens. Works for both "expired cached tokens" and "no tokens yet"
        # as long as the Chrome profile has a saved Google login.
        refreshed = _headless_reauth_via_login_module()
        if refreshed is not None:
            return refreshed

        # Could not refresh automatically — give an actionable, honest error.
        if cached:
            _usable, reason = _auth_is_usable()
            return error_result(
                "Cached auth tokens are no longer valid and headless re-auth via "
                "the nlm login module failed (no saved Chrome login, or the saved "
                f"Google session has itself expired; reason: {reason}). "
                "Run `nlm login` in a terminal to re-authenticate interactively.",
                status="expired",
                reason=reason,
            )

        return {
            "status": "error",
            "error": (
                "No cached tokens found and headless re-auth via the nlm login "
                "module was not possible (no saved Chrome login). "
                "Run 'nlm login' to authenticate."
            ),
        }
    except Exception as e:
        return error_result(str(e))


@logged_tool()
def save_auth_tokens(
    cookies: str,
    csrf_token: str = "",
    session_id: str = "",
    request_body: str = "",
    request_url: str = "",
) -> ResultDict:
    """Save NotebookLM cookies (FALLBACK method - try `nlm login` first!).

    IMPORTANT FOR AI ASSISTANTS:
    - First, run `nlm login` via Bash/terminal (automated, preferred)
    - Only use this tool if the automated CLI fails

    Args:
        cookies: Cookie header from Chrome DevTools (only needed if CLI fails)
        csrf_token: Deprecated - auto-extracted
        session_id: Deprecated - auto-extracted
        request_body: Optional - contains CSRF if extracting manually
        request_url: Optional - contains session ID if extracting manually
    """
    try:
        from notebooklm_tools.services.auth import (
            AuthTokens,
            get_cache_path,
            save_tokens_to_cache,
        )

        # Parse cookie string to dict
        all_cookies = {}
        for part in cookies.split("; "):
            if "=" in part:
                key, value = part.split("=", 1)
                all_cookies[key.strip()] = value

        # Validate required cookies
        required = ["SID", "HSID", "SSID", "APISID", "SAPISID"]
        missing = [c for c in required if c not in all_cookies]
        if missing:
            return {
                "status": "error",
                "error": f"Missing required cookies: {missing}",
            }

        # Filter to only essential cookies
        cookie_dict = {k: v for k, v in all_cookies.items() if k in ESSENTIAL_COOKIES}

        # Try to extract CSRF token from request body if provided
        if not csrf_token and request_body and "at=" in request_body:
            at_part = request_body.split("at=")[1].split("&")[0]
            csrf_token = urllib.parse.unquote(at_part)

        # Try to extract session ID from request URL if provided
        if not session_id and request_url and "f.sid=" in request_url:
            sid_part = request_url.split("f.sid=")[1].split("&")[0]
            session_id = urllib.parse.unquote(sid_part)

        # Try to extract build label from request URL if provided
        build_label = ""
        if request_url and "bl=" in request_url:
            bl_part = request_url.split("bl=")[1].split("&")[0]
            build_label = urllib.parse.unquote(bl_part)

        # Create and save tokens
        tokens = AuthTokens(
            cookies=cookie_dict,
            csrf_token=csrf_token,
            session_id=session_id,
            build_label=build_label,
            extracted_at=time.time(),
        )
        save_tokens_to_cache(tokens)

        # Reset client so next call uses fresh tokens
        reset_client()

        # Build status message
        if csrf_token and session_id:
            token_msg = "CSRF token and session ID extracted from network request - no page fetch needed! ⚡"
        elif csrf_token:
            token_msg = "CSRF token extracted from network request. Session ID will be auto-extracted on first use."
        elif session_id:
            token_msg = "Session ID extracted from network request. CSRF token will be auto-extracted on first use."
        else:
            token_msg = "CSRF token and session ID will be auto-extracted on first API call (~1-2s one-time delay)."

        return {
            "status": "success",
            "message": f"Saved {len(cookie_dict)} essential cookies (filtered from {len(all_cookies)}). {token_msg}",
            "cache_path": str(get_cache_path()),
            "extracted_csrf": bool(csrf_token),
            "extracted_session_id": bool(session_id),
        }
    except Exception as e:
        return error_result(str(e))
