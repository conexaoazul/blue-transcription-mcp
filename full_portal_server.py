"""Full Blue Transcription MCP facade for Cloudflare MCP Portal origin.

Authentication is terminated by Cloudflare Access. This process is reachable only
through the private WireGuard-bound host port used by Traefik.
"""
import os

import uvicorn
from starlette.responses import JSONResponse

import server as core

app = core.mcp.streamable_http_app()


async def healthz(_request):
    return JSONResponse({"status": "ok", "mode": "portal-full", "tools": 8})


app.add_route("/healthz", healthz, methods=["GET"])

if __name__ == "__main__":
    core._ensure_output_dir()
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8086")))
