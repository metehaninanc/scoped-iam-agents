"""Local admin endpoint for the harness. Not part of the MCP app and not on the agents' port.

It binds the active ticket. Agents hold only an MCP token, and the admin token differs from
every agent token, so no agent can call it even if it could reach the port.
"""

import hmac

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .auth import parse_bearer
from .core import Gateway


def create_admin_app(gateway: Gateway) -> Starlette:
    expected = gateway.settings.admin_token

    def authorised(request: Request) -> bool:
        presented = parse_bearer(request.headers.get("authorization"))
        return bool(
            expected and presented and hmac.compare_digest(presented.encode(), expected.encode())
        )

    async def bind(request: Request):
        if not authorised(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        ticket_id = body.get("ticket_id")
        if not isinstance(ticket_id, str) or not ticket_id:
            return JSONResponse({"error": "ticket_id required"}, status_code=400)
        gateway.bind_ticket(ticket_id)
        return JSONResponse({"active_ticket": ticket_id})

    async def unbind(request: Request):
        if not authorised(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        gateway.unbind_ticket()
        return JSONResponse({"active_ticket": None})

    async def status(request: Request):
        if not authorised(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"active_ticket": gateway.active_ticket, "mode": gateway.settings.mode})

    return Starlette(
        routes=[
            Route("/admin/bind", bind, methods=["POST"]),
            Route("/admin/unbind", unbind, methods=["POST"]),
            Route("/admin/status", status, methods=["GET"]),
        ]
    )
