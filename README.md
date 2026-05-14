# MiniappsAI-API

MiniappsAI-API is a reverse-engineered Python client for the MiniApps.ai web app.
It is organized as a small package, with a Claude-API-style split between the
public package entry point, shared constants, exceptions, and the concrete
client implementation.

This repository focuses on the pieces that matter for scripting and integration:
authenticated REST requests, Socket.IO chat streaming, session persistence, and
the HAR-derived details needed to talk to the same backend the browser uses.

## What This Project Gives You

- Authenticated access to the MiniApps.ai REST API.
- Full login, refresh, logout, and current-user flows.
- Persistent cookie jar support so you can reuse sessions across runs.
- REST helpers for tools, conversations, reactions, favorites, feed,
	recommendations, personas, notifications, preferences, and settings.
- Real-time chat streaming over Socket.IO.
- A synchronous client for scripts and a thin async wrapper for asyncio code.
- A package layout that is easier to embed into other projects such as ChatAI
	Console later.

## Package Layout

```text
MiniappsAI-API/
├── miniapps_api/
│   ├── __init__.py        # Public package exports
│   ├── client.py          # Main client implementation + CLI demo
│   ├── constants.py       # Shared base URLs, headers, socket details
│   └── exceptions.py      # API exception types
└── README.md              # This guide
```

The preferred import path is:

```python
from miniapps_api import MiniAppsClient
```

## Installation

Python 3.10 or newer is recommended.

```sh
pip install requests python-socketio websocket-client
```

If you want to work on the package itself, install it in editable mode from the
repository root:

```sh
pip install -e .
```

## Quick Start

```python
from miniapps_api import MiniAppsClient

client = MiniAppsClient(cookie_file="miniapps_cookies.txt")

client.google_login("YOUR_GOOGLE_ID_TOKEN")
client.setup_user("myusername", "MyPassword123", client.last_auth_hash)

reply = client.chat("claude-37", "Tell me a joke")
print(reply)
```

If you prefer async usage:

```python
import asyncio
from miniapps_api import AsyncMiniAppsClient

async def main():
		async with AsyncMiniAppsClient(cookie_file="miniapps_cookies.txt") as client:
				await client.google_login("YOUR_GOOGLE_ID_TOKEN")
				reply = await client.chat("claude-37", "Tell me a joke")
				print(reply)

asyncio.run(main())
```

## Authentication

The client uses the same browser-authenticated session model as the web app.
The important pieces are the Google login flow, the session cookie jar, and the
Socket.IO auth token returned by the auth endpoints. The `google_login(id_token)`
call expects a Google ID token from that browser OAuth flow, not browser cookies.

### Typical Auth Flow

1. Obtain a Google ID token from the MiniApps.ai sign-in flow.
2. Call `google_login(id_token)`.
3. For new accounts, call `setup_user(username, password, hash_)`.
4. For existing sessions, call `me()` to refresh the current user data and the
	 Socket.IO token.
5. Reuse the saved cookie jar on future runs.

### Session Persistence

Pass `cookie_file=` when constructing the client. The cookie jar is loaded at
startup and saved after each mutating request.

```python
client = MiniAppsClient(cookie_file="miniapps_cookies.txt")
```

### CSRF Handling

The client automatically extracts the CSRF token from cookies and sends it on
every request. You do not need to manage `x-csrf-token` manually.

## Websocket / Streaming Notes

The HAR capture in [ws.har](ws.har) shows the browser connecting to:

```text
wss://api.miniapps.ai/socket.io/?EIO=4&transport=websocket
```

Observed browser behavior:

- The websocket uses the regular MiniApps cookies.
- The Socket.IO connection is authenticated with the `w` token returned by
	`/auth/me` or `/auth/setup/user`.
- The browser uses websocket transport directly.
- The Origin header is `https://miniapps.ai`.

Relevant constants are defined in [miniapps_api/constants.py](miniapps_api/constants.py).

## Main API Surface

### Client Classes

- `MiniAppsClient` - synchronous client for scripts and services.
- `AsyncMiniAppsClient` - thin async wrapper around the synchronous client.

### Exceptions

- `MiniAppsError` - generic API error.
- `InsufficientCreditsError` - raised when MiniApps reports HTTP 412.

### Useful Methods

- `csrf_check()`
- `google_login(id_token)`
- `setup_user(username, password, hash_)`
- `me()`
- `refresh()`
- `logout()`
- `get_tool_by_slug(slug, lang="en")`
- `get_conversation(conversation_id)`
- `send_message_raw(...)`
- `chat(...)`
- `chat_stream(...)`
- `abort_chat(conversation_id, request_id)`
- `get_feed(...)`
- `get_recommendations(...)`
- `get_user_preferences()`
- `get_personas()`
- `get_notification_count()`
- `get_maintenance_settings()`

## Chat Examples

### Simple Blocking Chat

```python
from miniapps_api import MiniAppsClient

client = MiniAppsClient(cookie_file="miniapps_cookies.txt")
reply = client.chat("claude-37", "Explain recursion simply")
print(reply)
```

### Continue a Conversation

```python
reply1 = client.chat("claude-37", "My name is Alice")
reply2 = client.chat(
		"claude-37",
		"What is my name?",
		conversation_id=client.last_conversation_id,
)
print(reply2)
```

### Streaming Tokens as They Arrive

```python
for fragment in client.chat_stream("claude-37", "Write a haiku about snow"):
		print(fragment, end="", flush=True)
```

### Async Streaming

```python
import asyncio
from miniapps_api import AsyncMiniAppsClient

async def main():
		async with AsyncMiniAppsClient() as client:
				async for fragment in client.chat_stream("claude-37", "Write a haiku"):
						print(fragment, end="", flush=True)

asyncio.run(main())
```

## Authentication Examples

### Existing Session

If you already have cookies saved, you can simply load the client and call
`me()`:

```python
client = MiniAppsClient(cookie_file="miniapps_cookies.txt")
user = client.me()
print(user)
```

### New Account Flow

```python
client = MiniAppsClient(cookie_file="miniapps_cookies.txt")
login = client.google_login("YOUR_GOOGLE_ID_TOKEN")
print(login)

setup = client.setup_user("myusername", "MyPassword123", client.last_auth_hash)
print(setup)
```

## Tool and Content Examples

### Fetch Tool Metadata

```python
tool = client.get_tool_by_slug("claude-37")
print(tool)
```

### Fetch a Conversation

```python
conversation = client.get_conversation("conversation-uuid")
print(conversation)
```

### Fetch Recommendations

```python
recommendations = client.get_recommendations(limit=10)
for item in recommendations:
		print(item["slug"], item.get("title"))
```

## CLI Demo

The main client module also contains a small CLI demo.

```sh
python -m miniapps_api.client --slug claude-37 --message "Hello"
```

Other examples:

```sh
python -m miniapps_api.client --info --slug claude-37
python -m miniapps_api.client --recent
python -m miniapps_api.client --recommendations
python -m miniapps_api.client --me
```

## Reverse-Engineered Details

The package is based on browser traffic captured from MiniApps.ai. That means
the API can change underneath it at any time. The current code assumes:

- The REST base URL is `https://api.miniapps.ai`.
- The websocket base URL is `wss://api.miniapps.ai`.
- Session auth is cookie-based.
- The Socket.IO connection uses the browser-origin pattern captured in the HAR.
- Some endpoints are derived from client-side traffic rather than official docs.

## Current Implementation Notes

- `miniapps_api/client.py` is the canonical implementation module.
- `miniapps_api/__init__.py` re-exports the public classes for clean imports.
- `miniapps_api/constants.py` centralizes base URLs, headers, and socket event
	names.
- `miniapps_api/exceptions.py` isolates error types.
- `ws.har` documents the websocket handshake used to inform the streaming code.

## Integration Notes for ChatAI Console

This package is shaped to make later integration easier:

- Import from `miniapps_api`, not a top-level script file.
- Keep auth/session handling separate from UI code.
- Use `MiniAppsClient` for blocking server-side flows.
- Use `AsyncMiniAppsClient` if you want asyncio-compatible orchestration.
- Preserve cookie persistence if you want the console to remember sessions.

## Troubleshooting

### Import Errors

If Python cannot import `socketio`, install the package dependencies:

```sh
pip install requests python-socketio websocket-client
```

### Authentication Errors

If `me()` or `chat()` fails with authentication errors, the most common causes
are:

- expired cookies,
- an invalid Google login token,
- a stale saved session,
- or a websocket token that needs to be refreshed with `me()`.

### Streaming Stops Early

If streaming stops or times out, the client falls back to fetching the final
conversation state when possible. That behavior is intentional and helps avoid
losing the assistant response when the socket drops.

## License

MIT
