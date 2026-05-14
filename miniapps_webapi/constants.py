"""Shared MiniApps API constants and websocket details."""

BASE_URL = "https://api.miniapps.ai"
WS_URL = "wss://api.miniapps.ai"
SOCKET_IO_PATH = "/socket.io/"

# Observed from ws.har: the browser connects with websocket transport only,
# sends the miniapps cookies, and authenticates the socket with the `w`
# token returned by /auth/me or /auth/setup/user.
SOCKET_IO_QUERY = "EIO=4&transport=websocket"

DEFAULT_HEADERS = {
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

SIOEVT_CHAT_TOKEN = "chat-token"
SIOEVT_CHAT_MESSAGE = "chat-message"
SIOEVT_CHAT_STATUS = "chat-status"
SIOEVT_CHAT_ERROR = "chat-error"
