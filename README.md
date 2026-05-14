# MiniappsAI-API

A reverse-engineered Python wrapper for the MiniApps.ai web app.

## Public API

Use the package entry point for new integrations:

```python
from miniapps_api import MiniAppsClient

client = MiniAppsClient(cookie_file="miniapps_cookies.txt")
```

The websocket trace in `ws.har` shows the browser connects to `wss://api.miniapps.ai/socket.io/?EIO=4&transport=websocket` with the regular MiniApps cookies, plus the socket auth token returned by `/auth/me` or `/auth/setup/user`.

## Notes

The current top-level `miniapps_client.py` remains as a compatibility entry point for existing scripts. The new `miniapps_api` package is the preferred import path for future work, including later ChatAI Console integration.
