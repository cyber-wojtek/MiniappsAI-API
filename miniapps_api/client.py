"""
client.py — Python client for the MiniApps.ai API
============================================================
Reverse-engineered from HAR captures.

Features
--------
* Full auth flow (Google OAuth, username/password setup, refresh, logout)
* CSRF token management (auto-extracted from cookies, auto-sent on every request)
* Session persistence (save / load cookie jar to disk)
* All documented REST endpoints (tools, conversations, chat, reactions, favorites,
  feed, recommendations, personas, notifications, user preferences, settings)
* Real-time chat streaming via Socket.IO (the channel the browser uses)
* Synchronous blocking `chat()` helper for simple script use
* Async `chat_async()` for use inside asyncio programs

Quick-start
-----------
    from miniapps_api import MiniAppsClient

    client = MiniAppsClient()
    client.google_login("YOUR_GOOGLE_ID_TOKEN")
    client.setup_user("myusername", "MyPassword123", client.last_auth_hash)

    # one-shot chat
    reply = client.chat("claude-37", "Tell me a joke")
    print(reply)

    # continue the same conversation
    reply2 = client.chat("claude-37", "Explain it to a 5-year-old",
                          conversation_id=client.last_conversation_id)
    print(reply2)

Dependencies
------------
    pip install requests python-socketio websocket-client
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from http.cookiejar import LWPCookieJar
from pathlib import Path
from threading import Event, Lock
from typing import TYPE_CHECKING, Callable, Optional
from urllib.parse import urlencode
import requests

if TYPE_CHECKING:
    import socketio

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://api.miniapps.ai"
WS_URL = "wss://api.miniapps.ai"

_DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://miniapps.ai",
    "Referer": "https://miniapps.ai/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

# Socket.IO event names observed / inferred from the abort-channel payload
_SIOEVT_CHAT_TOKEN = "chat-token"        # partial AI token
_SIOEVT_CHAT_MESSAGE = "chat-message"    # full message object (done)
_SIOEVT_CHAT_STATUS = "chat-status"      # phase updates
_SIOEVT_CHAT_ERROR = "chat-error"        # error


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class MiniAppsError(Exception):
    """Raised when the API returns an error status."""

    def __init__(self, status_code: int, message: str, body: dict | None = None):
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.body = body or {}


class InsufficientCreditsError(MiniAppsError):
    """Raised when the account has run out of credits (HTTP 412)."""


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------

class MiniAppsClient:
    """
    Synchronous client for the MiniApps.ai REST + Socket.IO API.

    Parameters
    ----------
    cookie_file:
        Path to a file used to persist the cookie jar between runs.
        If the file exists it is loaded automatically; cookies are saved
        automatically after every mutating request.
    timeout:
        Default HTTP request timeout in seconds.
    log_level:
        Logging level for this module (e.g. ``logging.DEBUG``).
    """

    def __init__(
        self,
        cookie_file: str | Path | None = None,
        timeout: int = 30,
        log_level: int = logging.WARNING,
    ):
        logging.basicConfig(level=log_level)

        self._timeout = timeout
        self._cookie_file = Path(cookie_file) if cookie_file else None
        self._ws_token: str | None = None   # JWT `w` from login/me
        self._csrf_token: str | None = None

        # Thread-safety for the socket connection
        self._sio_lock = Lock()
        self._sio: "socketio.Client | None" = None

        # Mutable state for convenience
        self.last_conversation_id: str | None = None
        self.last_auth_hash: str | None = None
        self.current_user: dict | None = None

        # Build requests session
        self._session = requests.Session()
        self._session.headers.update(_DEFAULT_HEADERS)

        if self._cookie_file and self._cookie_file.exists():
            jar = LWPCookieJar(str(self._cookie_file))
            jar.load()
            self._session.cookies = jar  # type: ignore[assignment]
            self._refresh_csrf_from_cookies()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refresh_csrf_from_cookies(self) -> None:
        """Pull the CSRF token out of the cookie jar."""
        for cookie in self._session.cookies:
            if cookie.name.lower() in ("x-csrf-token", "csrf-token", "csrftoken"):
                self._csrf_token = cookie.value
                return

    def _headers(self, extra: dict | None = None) -> dict:
        h: dict = {}
        if self._csrf_token:
            h["x-csrf-token"] = self._csrf_token
        if extra:
            h.update(extra)
        return h

    def _save_cookies(self) -> None:
        if self._cookie_file:
            jar = LWPCookieJar(str(self._cookie_file))
            for cookie in self._session.cookies:
                jar.set_cookie(cookie)
            jar.save()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        extra_headers: dict | None = None,
        stream: bool = False,
    ) -> requests.Response:
        url = f"{BASE_URL}{path}"
        headers = self._headers(extra_headers)
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        resp = self._session.request(
            method,
            url,
            params=params,
            json=json_body,
            headers=headers,
            timeout=self._timeout,
            stream=stream,
        )

        # Refresh CSRF after any response (server may rotate it)
        self._refresh_csrf_from_cookies()
        self._save_cookies()

        if not resp.ok:
            try:
                body = resp.json()
            except Exception:
                body = {"raw": resp.text}
            msg = body.get("message", resp.text)
            if resp.status_code == 412:
                raise InsufficientCreditsError(resp.status_code, msg, body)
            raise MiniAppsError(resp.status_code, msg, body)

        return resp

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        resp = self._request("GET", path, params=params)
        if not resp.content:
            return {}
        return resp.json()

    def _post(self, path: str, body: dict | None = None) -> dict | list:
        resp = self._request("POST", path, json_body=body or {})
        if not resp.content:
            return {}
        return resp.json()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def csrf_check(self) -> dict:
        """
        POST /auth/csrfCheck
        Validates the current CSRF token with the server.
        """
        return self._post("/auth/csrfCheck")  # type: ignore[return-value]

    def google_login(self, id_token: str) -> dict:
        """
        POST /auth/google/login
        Exchange a Google OAuth ``id_token`` for a MiniApps session.

        Returns the raw response dict which includes ``hash`` (needed by
        :meth:`setup_user` for new accounts) and ``status``.

        Parameters
        ----------
        id_token:
            The raw Google ID token obtained from the Google Identity
            Services SDK / OAuth flow.
        """
        resp = self._post("/auth/google/login", {"idToken": id_token})
        self.last_auth_hash = resp.get("hash")  # type: ignore[union-attr]
        return resp  # type: ignore[return-value]

    def setup_user(self, username: str, password: str, hash_: str) -> dict:
        """
        POST /auth/setup/user
        Complete registration for a new Google-linked account.

        Parameters
        ----------
        username:
            Desired display name / username.
        password:
            Account password.
        hash_:
            The ``hash`` value returned by :meth:`google_login`.
        """
        resp = self._post(
            "/auth/setup/user",
            {"username": username, "password": password, "hash": hash_},
        )
        self._ws_token = resp.get("w")  # type: ignore[union-attr]
        self.current_user = resp.get("user")  # type: ignore[union-attr]
        return resp  # type: ignore[return-value]

    def me(self, auth_context: str | None = None, auth_source: str | None = None) -> dict:
        """
        GET /auth/me
        Return the currently authenticated user object.

        The response also contains the Socket.IO WebSocket token (``w``),
        which is stored automatically on this client instance.
        """
        params: dict = {}
        if auth_context:
            params["authContext"] = auth_context
        if auth_source:
            params["authSource"] = auth_source

        resp = self._get("/auth/me", params or None)
        self._ws_token = resp.get("w")  # type: ignore[union-attr]
        self.current_user = resp.get("user")  # type: ignore[union-attr]
        return resp  # type: ignore[return-value]

    def refresh(self) -> dict:
        """
        POST /auth/refresh
        Refresh the current session (rotates the auth cookie).
        """
        return self._post("/auth/refresh")  # type: ignore[return-value]

    def logout(self) -> dict:
        """
        POST /auth/logout
        Invalidate the current session on the server and clear local cookies.
        """
        resp = self._post("/auth/logout")
        self._session.cookies.clear()
        self._csrf_token = None
        self._ws_token = None
        self.current_user = None
        self._save_cookies()
        return resp  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Tools (AI chatbot / app definitions)
    # ------------------------------------------------------------------

    def get_tool_by_slug(self, slug: str, lang: str = "en") -> dict:
        """
        GET /tools/s/{slug}
        Fetch full tool metadata by its URL slug (e.g. ``"claude-37"``).
        """
        return self._get(f"/tools/s/{slug}", {"lang": lang})  # type: ignore[return-value]

    def get_similar_tools(self, tool_id: str, lang: str = "en", nsfw: int = 0) -> list:
        """
        GET /tools/{tool_id}/similar
        Return tools similar to the given one.
        """
        return self._get(f"/tools/{tool_id}/similar", {"lang": lang, "nsfw": nsfw})  # type: ignore[return-value]

    def get_tool_conversations(
        self,
        tool_id: str,
        page: int = 1,
        items_per_page: int = 10,
        sort_by: list[str] | None = None,
        sort_desc: list[bool] | None = None,
    ) -> dict:
        """
        GET /tools/{tool_id}/conversations
        List conversations the current user has had with this tool.
        """
        sort_by = sort_by or ["pinned", "updatedAt"]
        sort_desc = sort_desc or [True, True]
        options = json.dumps(
            {
                "itemsPerPage": items_per_page,
                "page": page,
                "sortBy": sort_by,
                "sortDesc": sort_desc,
                "mustSort": True,
            }
        )
        return self._get(f"/tools/{tool_id}/conversations", {"options": options})  # type: ignore[return-value]

    def get_resumed_tools(
        self,
        tag: str | None = None,
        lang: str = "en",
        nsfw: int = 0,
        page: int = 1,
        items_per_page: int = 16,
    ) -> dict:
        """
        GET /tools/resumed
        Discover tools, optionally filtered by a tag.
        """
        options = json.dumps(
            {
                "mustSort": True,
                "sortBy": ["similarity", "generationsWeek"],
                "sortDesc": [True, True],
                "page": page,
                "itemsPerPage": items_per_page,
                "lang": lang,
            }
        )
        params: dict = {"options": options, "nsfw": nsfw}
        if tag:
            params["tag"] = tag
        return self._get("/tools/resumed", params)  # type: ignore[return-value]

    def get_favorite_tools(
        self,
        lang: str = "en",
        page: int = 1,
        items_per_page: int = 20,
    ) -> dict:
        """
        GET /tools/favorites
        Return the current user's bookmarked tools.
        """
        options = json.dumps(
            {
                "sortBy": ["favorite.createdAt"],
                "sortDesc": [True],
                "mustSort": True,
                "lang": lang,
                "itemsPerPage": items_per_page,
            }
        )
        return self._get("/tools/favorites", {"options": options})  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------

    def get_conversation(self, conversation_id: str) -> dict:
        """
        GET /conversations/{conversation_id}
        Fetch a conversation with its full message history.
        """
        return self._get(f"/conversations/{conversation_id}")  # type: ignore[return-value]

    def get_quick_access_conversations(
        self,
        lang: str = "en",
        page: int = 1,
        items_per_page: int = 20,
    ) -> dict:
        """
        GET /conversations/quickAccess
        Return the most recently active conversations for quick navigation.
        """
        options = json.dumps({"itemsPerPage": items_per_page, "page": page, "lang": lang})
        return self._get("/conversations/quickAccess", {"options": options})  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Chat messages
    # ------------------------------------------------------------------

    def get_message_alternatives(
        self, conversation_id: str, message_id: str
    ) -> dict:
        """
        GET /chat-messages/{conversation_id}/{message_id}/alternatives
        Return the list of alternative (regenerated) versions for a message.
        """
        return self._get(  # type: ignore[return-value]
            f"/chat-messages/{conversation_id}/{message_id}/alternatives"
        )

    # ------------------------------------------------------------------
    # Chat — sending messages
    # ------------------------------------------------------------------

    def send_message_raw(
        self,
        tool_id: str,
        revision: int,
        model_id: str,
        text: str,
        *,
        conversation_id: str | None = None,
        language: str = "en",
        elements: list[dict] | None = None,
        request_id: str | None = None,
    ) -> dict:
        """
        POST /chat
        Send a user message and create (or continue) a conversation.

        This is the **low-level** method that returns the raw API response.
        It does *not* wait for the AI response — that arrives via Socket.IO.
        For a blocking call that returns the AI reply use :meth:`chat`.

        Parameters
        ----------
        tool_id:
            UUID of the tool/chatbot to use.
        revision:
            Tool revision (almost always ``1``).
        model_id:
            UUID of the underlying AI model.
        text:
            User message text.
        conversation_id:
            Pass to continue an existing conversation; omit to start a new one.
        language:
            BCP-47 language tag (default ``"en"``).
        elements:
            Override the default ``[{"type": "text", "text": text}]`` message
            structure, e.g. to attach images.
        request_id:
            Client-generated UUID4 used to track / abort this request.
            Auto-generated if not provided.

        Returns
        -------
        dict
            Contains ``chatMessageId``, ``conversationId``,
            ``requestId``, ``conversation``, ``addedMessages``.
        """
        if not request_id:
            request_id = str(uuid.uuid4())
        if elements is None:
            elements = [{"type": "text", "text": text}]

        body: dict = {
            "toolId": tool_id,
            "revision": revision,
            "modelId": model_id,
            "requestId": request_id,
            "elements": elements,
            "language": language,
        }
        if conversation_id:
            body["conversationId"] = conversation_id

        resp = self._post("/chat", body)
        self.last_conversation_id = resp.get("conversationId")  # type: ignore[union-attr]
        return resp  # type: ignore[return-value]

    def abort_chat(self, conversation_id: str, request_id: str) -> dict:
        """
        POST /chat/abort
        Stop an in-progress AI generation.

        Parameters
        ----------
        conversation_id:
            ID of the conversation to abort.
        request_id:
            The ``requestId`` that was passed to :meth:`send_message_raw`.
        """
        return self._post(  # type: ignore[return-value]
            "/chat/abort",
            {"conversationId": conversation_id, "requestId": request_id},
        )

    # ------------------------------------------------------------------
    # Reactions
    # ------------------------------------------------------------------

    def get_tool_reactions(
        self,
        tool_id: str,
        page: int = 1,
        items_per_page: int = 10,
        sort_by: list[str] | None = None,
        sort_desc: list[bool] | None = None,
    ) -> dict:
        """
        GET /reactions/t/{tool_id}
        List community reactions (reviews/ratings) for a tool.
        """
        sort_by = sort_by or ["createdAt"]
        sort_desc = sort_desc or [True]
        options = json.dumps(
            {
                "page": page,
                "itemsPerPage": items_per_page,
                "sortBy": sort_by,
                "sortDesc": sort_desc,
            }
        )
        return self._get(f"/reactions/t/{tool_id}", {"options": options})  # type: ignore[return-value]

    def get_my_reaction(self, tool_id: str) -> dict:
        """
        GET /reactions/t/{tool_id}/me
        Return the current user's reaction to the given tool.
        """
        return self._get(f"/reactions/t/{tool_id}/me")  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Favorites
    # ------------------------------------------------------------------

    def is_favorite(self, tool_id: str) -> bool:
        """
        GET /favorites/isFavorite/{tool_id}
        Check whether the current user has bookmarked this tool.
        """
        resp = self._get(f"/favorites/isFavorite/{tool_id}")
        return bool(resp.get("fav"))  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Feed
    # ------------------------------------------------------------------

    def get_feed(
        self,
        tool_id: str | None = None,
        mode: str = "new",
        period: str = "always",
        lang: str = "en",
        page: int = 1,
        items_per_page: int = 20,
    ) -> dict:
        """
        GET /feed
        Fetch community posts / conversation shares.

        Parameters
        ----------
        tool_id:
            Filter to posts related to a specific tool.
        mode:
            ``"new"`` | ``"top"`` etc.
        period:
            ``"always"`` | ``"day"`` | ``"week"`` | ``"month"``.
        """
        options = json.dumps({"itemsPerPage": items_per_page, "page": page, "lang": lang})
        params: dict = {"options": options, "mode": mode, "period": period}
        if tool_id:
            params["toolId"] = tool_id
        return self._get("/feed", params)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Recommendations
    # ------------------------------------------------------------------

    def get_recommendations(
        self, limit: int = 20, lang: str = "en", nsfw: int = 0
    ) -> list:
        """
        GET /recommendations
        Return curated tool recommendations for the current user.
        """
        return self._get("/recommendations", {"limit": limit, "lang": lang, "nsfw": nsfw})  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # User
    # ------------------------------------------------------------------

    def get_user_preferences(self) -> list:
        """
        GET /user/preferences
        Return the current user's saved UI/chat preferences.
        """
        return self._get("/user/preferences")  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Personas
    # ------------------------------------------------------------------

    def get_personas(self) -> list:
        """
        GET /personas
        Return personas (custom system-prompt identities) created by the user.
        """
        return self._get("/personas")  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def get_notification_count(self) -> int:
        """
        GET /notifications/count
        Return the number of unread notifications.
        """
        resp = self._get("/notifications/count")
        return int(resp.get("count", 0))  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def get_maintenance_settings(self) -> dict:
        """
        GET /settings/maintenance
        Check whether the platform is in maintenance mode.
        """
        return self._get("/settings/maintenance")  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Socket.IO streaming
    # ------------------------------------------------------------------

    def _ensure_socket(self) -> "socketio.Client":
        """Create and connect the Socket.IO client if not already connected."""

        with self._sio_lock:
            if self._sio and self._sio.connected:
                return self._sio

            sio = socketio.Client(
                logger=False,
                engineio_logger=False,
                reconnection=True,
                reconnection_attempts=3,
            )
            auth: dict = {}
            if self._ws_token:
                auth["token"] = self._ws_token

            # Build cookie string to pass as a header
            cookie_str = "; ".join(
                f"{c.name}={c.value}" for c in self._session.cookies
            )
            extra_headers = {
                "Cookie": cookie_str,
                "Origin": "https://miniapps.ai",
            }

            sio.connect(
                WS_URL,
                socketio_path="/socket.io/",
                transports=["websocket"],
                auth=auth,
                headers=extra_headers,
                wait_timeout=10,
            )
            self._sio = sio
            return sio

    def disconnect_socket(self) -> None:
        """Cleanly disconnect the Socket.IO connection."""
        with self._sio_lock:
            if self._sio and self._sio.connected:
                self._sio.disconnect()
            self._sio = None

    def chat_stream(
        self,
        tool_slug_or_id: str,
        text: str,
        *,
        conversation_id: str | None = None,
        language: str = "en",
        on_token: Callable[[str], None] | None = None,
        timeout: int = 120,
        is_slug: bool = True,
    ) -> str:
        """
        Send a message and stream the AI reply via Socket.IO.

        Parameters
        ----------
        tool_slug_or_id:
            URL slug (e.g. ``"claude-37"``) or UUID of the tool.
            Set ``is_slug=False`` when passing a UUID directly.
        text:
            User message text.
        conversation_id:
            Pass to continue an existing conversation.
        language:
            BCP-47 language tag.
        on_token:
            Optional callback invoked with each streamed text fragment as it
            arrives.  Signature: ``on_token(fragment: str) -> None``.
        timeout:
            Maximum seconds to wait for the complete AI response.
        is_slug:
            ``True`` (default) when ``tool_slug_or_id`` is a slug.

        Returns
        -------
        str
            The complete AI response text.
        """
        # Resolve tool info
        if is_slug:
            tool = self.get_tool_by_slug(tool_slug_or_id, lang=language)
        else:
            # treat as id — caller must pass a known model_id separately;
            # we do a best-effort with the tool itself
            tool = {"id": tool_slug_or_id, "revision": 1, "modelId": None}
            logger.warning("No model_id supplied; the server will choose a default.")

        tool_id: str = tool["id"]
        revision: int = tool.get("revision", 1)
        model_id: str = tool.get("modelId", "")
        request_id = str(uuid.uuid4())

        # Accumulators
        full_text: list[str] = []
        done_event = Event()
        error_holder: list[Exception] = []

        # Connect Socket.IO
        sio = self._ensure_socket()

        # --- register event handlers for this request ---

        def on_token_event(data):
            """Partial token from the AI stream."""
            # data may be a string or dict
            fragment: str = ""
            if isinstance(data, str):
                fragment = data
            elif isinstance(data, dict):
                fragment = data.get("text", data.get("token", data.get("content", "")))
            # Filter to our conversation
            conv = data.get("conversationId") if isinstance(data, dict) else None
            if conv and conv != (conversation_id or self.last_conversation_id):
                return
            if fragment:
                full_text.append(fragment)
                if on_token:
                    on_token(fragment)

        def on_message_event(data):
            """Complete message object — signals end of stream."""
            if isinstance(data, dict):
                conv = data.get("conversationId")
                if conv and conv != (conversation_id or self.last_conversation_id):
                    return
                # If we haven't accumulated tokens, extract from message object
                if not full_text:
                    msg_text = data.get("text", "")
                    if not msg_text and data.get("elements"):
                        msg_text = " ".join(
                            e.get("text", "") for e in data["elements"]
                            if e.get("type") == "text"
                        )
                    if msg_text:
                        full_text.append(msg_text)
            done_event.set()

        def on_error_event(data):
            msg = data if isinstance(data, str) else data.get("message", str(data))
            error_holder.append(MiniAppsError(0, f"Stream error: {msg}"))
            done_event.set()

        def on_status_event(data):
            logger.debug("chat-status: %s", data)

        sio.on(_SIOEVT_CHAT_TOKEN, on_token_event)
        sio.on(_SIOEVT_CHAT_MESSAGE, on_message_event)
        sio.on(_SIOEVT_CHAT_STATUS, on_status_event)
        sio.on(_SIOEVT_CHAT_ERROR, on_error_event)

        # Also listen for done on a conversation-specific channel
        conv_channel = f"chat-done:{conversation_id}" if conversation_id else None
        if conv_channel:
            sio.on(conv_channel, lambda _: done_event.set())

        try:
            # Fire the HTTP request to start generation
            resp = self.send_message_raw(
                tool_id=tool_id,
                revision=revision,
                model_id=model_id,
                text=text,
                conversation_id=conversation_id,
                language=language,
                request_id=request_id,
            )
            actual_conv_id = resp.get("conversationId", conversation_id)
            self.last_conversation_id = actual_conv_id

            # Update channel listener now that we know the conv id
            done_channel = f"chat-done:{actual_conv_id}"
            sio.on(done_channel, lambda _: done_event.set())

            # Wait for the stream to finish
            finished = done_event.wait(timeout=timeout)
            if not finished:
                logger.warning(
                    "Stream timed out after %ss. Falling back to conversation fetch.",
                    timeout,
                )
                # Fallback: fetch conversation to get last message
                conv_data = self.get_conversation(actual_conv_id)
                messages = conv_data.get("messages", [])
                ai_msgs = [m for m in messages if m.get("origin") == "assistant"]
                if ai_msgs:
                    last = ai_msgs[-1]
                    text_parts = [
                        e.get("text", "")
                        for e in last.get("elements", [])
                        if e.get("type") == "text"
                    ]
                    return " ".join(text_parts) or last.get("text", "")

            if error_holder:
                raise error_holder[0]

            return "".join(full_text)

        finally:
            # Remove handlers to avoid leaking across calls
            for evt in [
                _SIOEVT_CHAT_TOKEN,
                _SIOEVT_CHAT_MESSAGE,
                _SIOEVT_CHAT_STATUS,
                _SIOEVT_CHAT_ERROR,
            ]:
                sio.on(evt, None)

    def chat(
        self,
        tool_slug: str,
        text: str,
        *,
        conversation_id: str | None = None,
        language: str = "en",
        print_stream: bool = False,
        timeout: int = 120,
    ) -> str:
        """
        High-level blocking chat helper.

        Resolves the tool, sends the message, waits for the full AI response
        via Socket.IO, and returns it as a string.

        Parameters
        ----------
        tool_slug:
            URL slug of the chatbot (e.g. ``"claude-37"``).
        text:
            Your message.
        conversation_id:
            Pass to continue an existing conversation.  After a successful
            call you can also pass ``client.last_conversation_id`` on the
            next turn.
        language:
            BCP-47 language tag (default ``"en"``).
        print_stream:
            If ``True``, print each token fragment to stdout as it arrives.
        timeout:
            Seconds to wait before falling back to a conversation fetch.

        Returns
        -------
        str
            The complete AI response.
        """
        on_token = (lambda t: print(t, end="", flush=True)) if print_stream else None
        result = self.chat_stream(
            tool_slug,
            text,
            conversation_id=conversation_id,
            language=language,
            on_token=on_token,
            timeout=timeout,
        )
        if print_stream:
            print()  # newline after stream
        return result

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "MiniAppsClient":
        return self

    def __exit__(self, *_) -> None:
        self.disconnect_socket()
        self._session.close()


# ---------------------------------------------------------------------------
# Async wrapper (requires asyncio event loop)
# ---------------------------------------------------------------------------

class AsyncMiniAppsClient:
    """
    Thin async wrapper around :class:`MiniAppsClient`.

    All methods delegate to the synchronous client but are exposed as
    coroutines so they can be used inside ``asyncio`` programs without
    blocking the event loop (via ``asyncio.to_thread``).

    Example
    -------
    ::

        import asyncio
        from miniapps_api import AsyncMiniAppsClient

        async def main():
            async with AsyncMiniAppsClient() as client:
                await client.google_login("TOKEN")
                reply = await client.chat("claude-37", "Hello!")
                print(reply)

        asyncio.run(main())
    """

    def __init__(self, **kwargs):
        import asyncio as _asyncio
        self._asyncio = _asyncio
        self._sync = MiniAppsClient(**kwargs)

    def _wrap(self, fn, *args, **kwargs):
        return self._asyncio.to_thread(fn, *args, **kwargs)

    async def google_login(self, id_token: str):
        return await self._wrap(self._sync.google_login, id_token)

    async def setup_user(self, username: str, password: str, hash_: str):
        return await self._wrap(self._sync.setup_user, username, password, hash_)

    async def me(self, **kwargs):
        return await self._wrap(self._sync.me, **kwargs)

    async def refresh(self):
        return await self._wrap(self._sync.refresh)

    async def logout(self):
        return await self._wrap(self._sync.logout)

    async def get_tool_by_slug(self, slug: str, lang: str = "en"):
        return await self._wrap(self._sync.get_tool_by_slug, slug, lang)

    async def get_conversation(self, conversation_id: str):
        return await self._wrap(self._sync.get_conversation, conversation_id)

    async def get_quick_access_conversations(self, **kwargs):
        return await self._wrap(self._sync.get_quick_access_conversations, **kwargs)

    async def get_recommendations(self, **kwargs):
        return await self._wrap(self._sync.get_recommendations, **kwargs)

    async def is_favorite(self, tool_id: str):
        return await self._wrap(self._sync.is_favorite, tool_id)

    async def get_feed(self, **kwargs):
        return await self._wrap(self._sync.get_feed, **kwargs)

    async def abort_chat(self, conversation_id: str, request_id: str):
        return await self._wrap(self._sync.abort_chat, conversation_id, request_id)

    async def chat(self, tool_slug: str, text: str, **kwargs) -> str:
        return await self._wrap(self._sync.chat, tool_slug, text, **kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self._sync.disconnect_socket()
        self._sync._session.close()


# ---------------------------------------------------------------------------
# CLI demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="MiniApps.ai chat CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples
--------
  # One-shot chat (uses saved cookies if present)
    python -m miniapps_api.client --slug claude-37 --message "What is the capital of France?"

  # Continue a conversation
    python -m miniapps_api.client --slug claude-37 \\
      --message "And what is its population?" \\
      --conversation-id <UUID>

  # Lookup tool info
    python -m miniapps_api.client --info --slug claude-37

  # Show recent conversations
    python -m miniapps_api.client --recent

  # Show recommendations
    python -m miniapps_api.client --recommendations
""",
    )
    parser.add_argument("--slug", default="claude-37", help="Tool slug (default: claude-37)")
    parser.add_argument("--message", "-m", help="Message to send")
    parser.add_argument("--conversation-id", help="Continue an existing conversation")
    parser.add_argument("--cookies", default="miniapps_cookies.txt", help="Cookie jar file")
    parser.add_argument("--info", action="store_true", help="Show tool info and exit")
    parser.add_argument("--recent", action="store_true", help="Show recent conversations")
    parser.add_argument("--recommendations", action="store_true", help="Show recommendations")
    parser.add_argument("--me", action="store_true", help="Show current user")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    log_level = logging.DEBUG if args.debug else logging.WARNING
    client = MiniAppsClient(cookie_file=args.cookies, log_level=log_level)

    if args.me:
        user_data = client.me()
        print(json.dumps(user_data, indent=2))
        sys.exit(0)

    if args.recommendations:
        recs = client.get_recommendations()
        for r in recs:
            print(f"  {r['title']:40s}  {r['slug']}")
        sys.exit(0)

    if args.recent:
        convs = client.get_quick_access_conversations()
        items = convs.get("items", [])
        if not items:
            print("No recent conversations.")
        for c in items:
            tool_title = c.get("tool", {}).get("title", "?")
            print(f"  [{c['id']}]  {tool_title}  —  {c.get('title') or '(untitled)'}")
        sys.exit(0)

    if args.info:
        tool = client.get_tool_by_slug(args.slug)
        print(json.dumps(tool, indent=2, default=str))
        sys.exit(0)

    if not args.message:
        parser.print_help()
        sys.exit(1)

    print(f"Chatting with {args.slug!r}…\n")
    try:
        reply = client.chat(
            args.slug,
            args.message,
            conversation_id=args.conversation_id,
            print_stream=True,
        )
        print(f"\n\n[conversation_id: {client.last_conversation_id}]")
    except InsufficientCreditsError as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(2)
    except MiniAppsError as e:
        print(f"\nAPI Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        client.disconnect_socket()
