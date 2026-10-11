"""MCP server (streamable HTTP) in front of the gateway core.

The bearer token picks the agent. A pure ASGI middleware rejects missing or unknown tokens with
401 before MCP sees the request, and each handler authenticates again from the same header, so
the tool list and every call depend only on the token.
"""

import json

import mcp_types as t
from mcp.server import ServerRequestContext
from mcp.server.lowlevel import Server
from starlette.responses import JSONResponse

from .auth import TokenRegistry
from .core import Gateway


class _BearerGate:
    """ASGI middleware: 401 unless the Authorization header carries a known agent token."""

    def __init__(self, app, registry: TokenRegistry, gateway: Gateway):
        self.app = app
        self.registry = registry
        self.gateway = gateway

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            if self.registry.authenticate(headers.get("authorization")) is None:
                self.gateway.audit_auth_failure("invalid_or_missing_token")
                response = JSONResponse({"error": "unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_mcp_app(gateway: Gateway):
    registry = TokenRegistry(gateway.settings.tokens)

    def agent_of(ctx: ServerRequestContext) -> str | None:
        headers = ctx.request.headers if ctx.request is not None else {}
        return registry.authenticate(headers.get("authorization"))

    async def on_list_tools(ctx, params):
        agent = agent_of(ctx)
        if agent is None:
            return t.ListToolsResult(tools=[])
        return t.ListToolsResult(
            tools=[
                t.Tool(name=d.name, description=d.description, input_schema=d.input_schema)
                for d in gateway.tools_for(agent)
            ]
        )

    async def on_call_tool(ctx, params):
        agent = agent_of(ctx)
        if agent is None:
            gateway.audit_auth_failure("invalid_or_missing_token")
            result = {"ok": False, "decision": "denied", "reason": "unauthenticated"}
        else:
            result = gateway.call(agent, params.name, dict(params.arguments or {}))
        return t.CallToolResult(content=[t.TextContent(type="text", text=json.dumps(result))])

    server = Server("iam-gateway", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    app = server.streamable_http_app(stateless_http=True, json_response=True)
    app.add_middleware(_BearerGate, registry=registry, gateway=gateway)
    return app
