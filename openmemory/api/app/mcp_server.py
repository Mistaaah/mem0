"""
MCP Server for OpenMemory with resilient memory client handling.

This module implements an MCP (Model Context Protocol) server that provides
memory operations for OpenMemory. The memory client is initialized lazily
to prevent server crashes when external dependencies (like Ollama) are
unavailable. If the memory client cannot be initialized, the server will
continue running with limited functionality and appropriate error messages.

Key features:
- Lazy memory client initialization
- Graceful error handling for unavailable dependencies
- Fallback to database-only mode when vector store is unavailable
- Proper logging for debugging connection issues
- Environment variable parsing for API keys
"""

import contextvars
import datetime
import json
import logging
import uuid

from app.database import SessionLocal
from app.models import Memory, MemoryAccessLog, MemoryState, MemoryStatusHistory
from app.utils.db import get_user_and_app
from app.utils.memory import get_memory_client
from app.utils.permissions import check_memory_access_permissions
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, status
from fastapi.routing import APIRouter
from mcp.server.fastmcp import FastMCP
from mcp.server.sse import SseServerTransport

# Load environment variables
load_dotenv()

# Initialize MCP
mcp = FastMCP("mem0-mcp-server")

# Don't initialize memory client at import time - do it lazily when needed
def get_memory_client_safe():
    """Get memory client with error handling. Returns None if client cannot be initialized."""
    try:
        return get_memory_client()
    except Exception as e:
        logging.warning(f"Failed to get memory client: {e}")
        return None

# Context variables for user_id and client_name
user_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("user_id")
client_name_var: contextvars.ContextVar[str] = contextvars.ContextVar("client_name")

# Create a router for MCP endpoints
mcp_router = APIRouter(prefix="/mcp")

# Initialize SSE transport
sse = SseServerTransport("/mcp/messages/")

@mcp.tool(description="Add a new memory. This method is called everytime the user informs anything about themselves, their preferences, or anything that has any relevant information which can be useful in the future conversation. This can also be called when the user asks you to remember something.")
async def add_memories(text: str) -> str:
    uid = user_id_var.get(None)
    client_name = client_name_var.get(None)

    if not uid:
        return "Error: user_id not provided"
    if not client_name:
        return "Error: client_name not provided"

    # Get memory client safely
    memory_client = get_memory_client_safe()
    if not memory_client:
        return "Error: Memory system is currently unavailable. Please try again later."

    try:
        db = SessionLocal()
        try:
            # Get or create user and app
            user, app = get_user_and_app(db, user_id=uid, app_id=client_name)

            # Check if app is active
            if not app.is_active:
                return f"Error: App {app.name} is currently paused on OpenMemory. Cannot create new memories."

            response = memory_client.add(text,
                                         user_id=uid,
                                         metadata={
                                            "source_app": "openmemory",
                                            "mcp_client": client_name,
                                        })

            # Process the response and update database
            if isinstance(response, dict) and 'results' in response:
                for result in response['results']:
                    memory_id = uuid.UUID(result['id'])
                    memory = db.query(Memory).filter(Memory.id == memory_id).first()

                    if result['event'] == 'ADD':
                        if not memory:
                            memory = Memory(
                                id=memory_id,
                                user_id=user.id,
                                app_id=app.id,
                                content=result['memory'],
                                state=MemoryState.active
                            )
                            db.add(memory)
                        else:
                            memory.state = MemoryState.active
                            memory.content = result['memory']

                        # Create history entry
                        history = MemoryStatusHistory(
                            memory_id=memory_id,
                            changed_by=user.id,
                            old_state=MemoryState.deleted if memory else None,
                            new_state=MemoryState.active
                        )
                        db.add(history)

                    elif result['event'] == 'DELETE':
                        if memory:
                            memory.state = MemoryState.deleted
                            memory.deleted_at = datetime.datetime.now(datetime.UTC)
                            # Create history entry
                            history = MemoryStatusHistory(
                                memory_id=memory_id,
                                changed_by=user.id,
                                old_state=MemoryState.active,
                                new_state=MemoryState.deleted
                            )
                            db.add(history)

                db.commit()

            return json.dumps(response)
        finally:
            db.close()
    except Exception as e:
        logging.exception(f"Error adding to memory: {e}")
        return f"Error adding to memory: {e}"


@mcp.tool(description="Search through stored memories. This method is called EVERYTIME the user asks anything.")
async def search_memory(query: str) -> str:
    uid = user_id_var.get(None)
    client_name = client_name_var.get(None)
    if not uid:
        return "Error: user_id not provided"
    if not client_name:
        return "Error: client_name not provided"

    # Get memory client safely
    memory_client = get_memory_client_safe()
    if not memory_client:
        return "Error: Memory system is currently unavailable. Please try again later."

    try:
        db = SessionLocal()
        try:
            # Get or create user and app
            user, app = get_user_and_app(db, user_id=uid, app_id=client_name)

            # Get accessible memory IDs based on ACL
            user_memories = db.query(Memory).filter(Memory.user_id == user.id).all()
            accessible_memory_ids = [memory.id for memory in user_memories if check_memory_access_permissions(db, memory, app.id)]

            filters = {
                "user_id": uid
            }

            embeddings = memory_client.embedding_model.embed(query, "search")

            hits = memory_client.vector_store.search(
                query=query, 
                vectors=embeddings, 
                limit=10, 
                filters=filters,
            )

            allowed = set(str(mid) for mid in accessible_memory_ids) if accessible_memory_ids else None

            results = []
            for h in hits:
                # All vector db search functions return OutputData class
                id, score, payload = h.id, h.score, h.payload
                if allowed and (h.id is None or h.id not in allowed):
                    continue
                
                results.append({
                    "id": id, 
                    "memory": payload.get("data"), 
                    "hash": payload.get("hash"),
                    "created_at": payload.get("created_at"), 
                    "updated_at": payload.get("updated_at"), 
                    "score": score,
                })

            for r in results: 
                if r.get("id"): 
                    access_log = MemoryAccessLog(
                        memory_id=uuid.UUID(r["id"]),
                        app_id=app.id,
                        access_type="search",
                        metadata_={
                            "query": query,
                            "score": r.get("score"),
                            "hash": r.get("hash"),
                        },
                    )
                    db.add(access_log)
            db.commit()

            return json.dumps({"results": results}, indent=2)
        finally:
            db.close()
    except Exception as e:
        logging.exception(e)
        return f"Error searching memory: {e}"


@mcp.tool(description="List all memories in the user's memory")
async def list_memories() -> str:
    uid = user_id_var.get(None)
    client_name = client_name_var.get(None)
    if not uid:
        return "Error: user_id not provided"
    if not client_name:
        return "Error: client_name not provided"

    # Get memory client safely
    memory_client = get_memory_client_safe()
    if not memory_client:
        return "Error: Memory system is currently unavailable. Please try again later."

    try:
        db = SessionLocal()
        try:
            # Get or create user and app
            user, app = get_user_and_app(db, user_id=uid, app_id=client_name)

            # Get all memories
            memories = memory_client.get_all(user_id=uid)
            filtered_memories = []

            # Filter memories based on permissions
            user_memories = db.query(Memory).filter(Memory.user_id == user.id).all()
            accessible_memory_ids = [memory.id for memory in user_memories if check_memory_access_permissions(db, memory, app.id)]
            if isinstance(memories, dict) and 'results' in memories:
                for memory_data in memories['results']:
                    if 'id' in memory_data:
                        memory_id = uuid.UUID(memory_data['id'])
                        if memory_id in accessible_memory_ids:
                            # Create access log entry
                            access_log = MemoryAccessLog(
                                memory_id=memory_id,
                                app_id=app.id,
                                access_type="list",
                                metadata_={
                                    "hash": memory_data.get('hash')
                                }
                            )
                            db.add(access_log)
                            filtered_memories.append(memory_data)
                db.commit()
            else:
                for memory in memories:
                    memory_id = uuid.UUID(memory['id'])
                    memory_obj = db.query(Memory).filter(Memory.id == memory_id).first()
                    if memory_obj and check_memory_access_permissions(db, memory_obj, app.id):
                        # Create access log entry
                        access_log = MemoryAccessLog(
                            memory_id=memory_id,
                            app_id=app.id,
                            access_type="list",
                            metadata_={
                                "hash": memory.get('hash')
                            }
                        )
                        db.add(access_log)
                        filtered_memories.append(memory)
                db.commit()
            return json.dumps(filtered_memories, indent=2)
        finally:
            db.close()
    except Exception as e:
        logging.exception(f"Error getting memories: {e}")
        return f"Error getting memories: {e}"


@mcp.tool(description="Delete specific memories by their IDs")
async def delete_memories(memory_ids: list[str]) -> str:
    uid = user_id_var.get(None)
    client_name = client_name_var.get(None)
    if not uid:
        return "Error: user_id not provided"
    if not client_name:
        return "Error: client_name not provided"

    # Get memory client safely
    memory_client = get_memory_client_safe()
    if not memory_client:
        return "Error: Memory system is currently unavailable. Please try again later."

    try:
        db = SessionLocal()
        try:
            # Get or create user and app
            user, app = get_user_and_app(db, user_id=uid, app_id=client_name)

            # Convert string IDs to UUIDs and filter accessible ones
            requested_ids = [uuid.UUID(mid) for mid in memory_ids]
            user_memories = db.query(Memory).filter(Memory.user_id == user.id).all()
            accessible_memory_ids = [memory.id for memory in user_memories if check_memory_access_permissions(db, memory, app.id)]

            # Only delete memories that are both requested and accessible
            ids_to_delete = [mid for mid in requested_ids if mid in accessible_memory_ids]

            if not ids_to_delete:
                return "Error: No accessible memories found with provided IDs"

            # Delete from vector store
            for memory_id in ids_to_delete:
                try:
                    memory_client.delete(str(memory_id))
                except Exception as delete_error:
                    logging.warning(f"Failed to delete memory {memory_id} from vector store: {delete_error}")

            # Update each memory's state and create history entries
            now = datetime.datetime.now(datetime.UTC)
            for memory_id in ids_to_delete:
                memory = db.query(Memory).filter(Memory.id == memory_id).first()
                if memory:
                    # Update memory state
                    memory.state = MemoryState.deleted
                    memory.deleted_at = now

                    # Create history entry
                    history = MemoryStatusHistory(
                        memory_id=memory_id,
                        changed_by=user.id,
                        old_state=MemoryState.active,
                        new_state=MemoryState.deleted
                    )
                    db.add(history)

                    # Create access log entry
                    access_log = MemoryAccessLog(
                        memory_id=memory_id,
                        app_id=app.id,
                        access_type="delete",
                        metadata_={"operation": "delete_by_id"}
                    )
                    db.add(access_log)

            db.commit()
            return f"Successfully deleted {len(ids_to_delete)} memories"
        finally:
            db.close()
    except Exception as e:
        logging.exception(f"Error deleting memories: {e}")
        return f"Error deleting memories: {e}"


@mcp.tool(description="Delete all memories in the user's memory")
async def delete_all_memories() -> str:
    uid = user_id_var.get(None)
    client_name = client_name_var.get(None)
    if not uid:
        return "Error: user_id not provided"
    if not client_name:
        return "Error: client_name not provided"

    # Get memory client safely
    memory_client = get_memory_client_safe()
    if not memory_client:
        return "Error: Memory system is currently unavailable. Please try again later."

    try:
        db = SessionLocal()
        try:
            # Get or create user and app
            user, app = get_user_and_app(db, user_id=uid, app_id=client_name)

            user_memories = db.query(Memory).filter(Memory.user_id == user.id).all()
            accessible_memory_ids = [memory.id for memory in user_memories if check_memory_access_permissions(db, memory, app.id)]

            # delete the accessible memories only
            for memory_id in accessible_memory_ids:
                try:
                    memory_client.delete(str(memory_id))
                except Exception as delete_error:
                    logging.warning(f"Failed to delete memory {memory_id} from vector store: {delete_error}")

            # Update each memory's state and create history entries
            now = datetime.datetime.now(datetime.UTC)
            for memory_id in accessible_memory_ids:
                memory = db.query(Memory).filter(Memory.id == memory_id).first()
                # Update memory state
                memory.state = MemoryState.deleted
                memory.deleted_at = now

                # Create history entry
                history = MemoryStatusHistory(
                    memory_id=memory_id,
                    changed_by=user.id,
                    old_state=MemoryState.active,
                    new_state=MemoryState.deleted
                )
                db.add(history)

                # Create access log entry
                access_log = MemoryAccessLog(
                    memory_id=memory_id,
                    app_id=app.id,
                    access_type="delete_all",
                    metadata_={"operation": "bulk_delete"}
                )
                db.add(access_log)

            db.commit()
            return "Successfully deleted all memories"
        finally:
            db.close()
    except Exception as e:
        logging.exception(f"Error deleting memories: {e}")
        return f"Error deleting memories: {e}"


@mcp_router.get("/{client_name}/sse/{user_id}")
async def handle_sse(request: Request):
    """Handle SSE connections for a specific user and client"""
    # Extract user_id and client_name from path parameters
    uid = request.path_params.get("user_id")
    user_token = user_id_var.set(uid or "")
    client_name = request.path_params.get("client_name")
    client_token = client_name_var.set(client_name or "")

    # Get api_key from query params OR X-API-KEY header (clients may pass via header)
    api_key = request.query_params.get("api_key") or request.headers.get("x-api-key")

    # Wrap _send to inject api_key into the messages endpoint URL the SDK
    # sends back to the client (e.g. data: /mcp/messages/?session_id=UUID).
    # Without this, the client POSTs to the messages URL without the key and
    # gets a 401 (shown as "Method Not Allowed" in some clients).
    async def send_with_api_key(message):
        if api_key and message.get("type") == "http.response.body":
            body = message.get("body", b"")
            if b"/mcp/messages/?session_id=" in body:
                body = body.replace(
                    b"/mcp/messages/?session_id=",
                    f"/mcp/messages/?api_key={api_key}&session_id=".encode(),
                )
                message = {**message, "body": body}
        await request._send(message)

    try:
        # Handle SSE connection
        async with sse.connect_sse(
            request.scope,
            request.receive,
            send_with_api_key,
        ) as (read_stream, write_stream):
            await mcp._mcp_server.run(
                read_stream,
                write_stream,
                mcp._mcp_server.create_initialization_options(),
            )
    finally:
        # Clean up context variables
        user_id_var.reset(user_token)
        client_name_var.reset(client_token)


@mcp_router.post("/messages/")
async def handle_global_message(request: Request):
    return await _handle_post_message_core(request)


@mcp_router.post("/{client_name}/sse/{user_id}/messages/")
async def handle_post_message_with_ids(request: Request, client_name: str, user_id: str):
    return await _handle_post_message_core(request)

async def _handle_post_message_core(request: Request):
    """Handle POST messages for SSE"""
    try:
        body = await request.body()
        
        # Captured response info for the ASGI send callback
        response_data = {"status": status.HTTP_204_NO_CONTENT, "headers": [], "body": b""}

        # Create a receive function for the MCP SDK to read the message body
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        # Create a send function for the MCP SDK to write the response info
        async def send(message):
            if message["type"] == "http.response.start":
                response_data["status"] = message["status"]
                response_data["headers"] = message.get("headers", [])
            elif message["type"] == "http.response.body":
                response_data["body"] += message.get("body", b"")

        # Delegate handling of the actual message to the MCP SSE transport
        await sse.handle_post_message(request.scope, receive, send)

        # Convert list of tuples (headers) to a dictionary safely
        response_headers = {}
        if response_data.get("headers"):
            for k, v in response_data["headers"]:
                try:
                    # Headers in ASGI are bytes, but we need strings for FastAPI Response
                    key = k.decode('latin-1') if isinstance(k, bytes) else str(k)
                    val = v.decode('latin-1') if isinstance(v, bytes) else str(v)
                    # Skip hop-by-hop or headers that FastAPI/Uvicorn manage themselves
                    if key.lower() not in ('content-length', 'content-type', 'transfer-encoding'):
                        response_headers[key] = val
                except Exception:
                    continue

        return Response(
            content=response_data.get("body") or b"",
            status_code=response_data.get("status") or 204,
            headers=response_headers or None
        )
    except Exception as e:
        import logging
        logging.exception(f"Error handling MCP post message: {e}")
        return Response(
            content=f"Internal Error: {e}".encode(),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

class _StreamableHTTPMiddleware:
    """
    ASGI middleware that sits in front of the FastMCP streamable-HTTP app.
    It strips the /mcp/{client_name}/http/{user_id} prefix, sets the async
    context-vars that the tool handlers read, and enforces the API key.
    """
    def __init__(self, asgi_app, expected_api_key: str | None):
        self.app = asgi_app
        self.expected_api_key = expected_api_key

    async def __call__(self, scope, receive, send):
        import os, secrets as _secrets
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")

        # Match /mcp/{client_name}/http/{user_id}
        import re
        m = re.match(r"^/mcp/([^/]+)/http/([^/]+)$", path)
        if not m:
            # Not our path — pass through unchanged
            await self.app(scope, receive, send)
            return

        client_name_val, user_id_val = m.group(1), m.group(2)

        # Auth: accept key from query-param or X-API-KEY header
        from urllib.parse import parse_qs
        qs = parse_qs(scope.get("query_string", b"").decode())
        provided_key = (qs.get("api_key", [None])[0] or
                        dict(scope.get("headers", [])).get(b"x-api-key", b"").decode() or None)

        if self.expected_api_key:
            if not provided_key or not _secrets.compare_digest(provided_key, self.expected_api_key):
                body = b'{"detail":"Unauthorized"}'
                await send({"type": "http.response.start", "status": 401,
                            "headers": [[b"content-type", b"application/json"],
                                        [b"content-length", str(len(body)).encode()]]})
                await send({"type": "http.response.body", "body": body})
                return

        # Rewrite path to /mcp so the inner Starlette app matches its route
        new_scope = dict(scope)
        new_scope["path"] = "/mcp"
        new_scope["raw_path"] = b"/mcp"

        # Set context vars; they propagate into child tasks created by the session manager
        user_token = user_id_var.set(user_id_val)
        client_token = client_name_var.set(client_name_val)
        try:
            await self.app(new_scope, receive, send)
        finally:
            user_id_var.reset(user_token)
            client_name_var.reset(client_token)


def setup_mcp_server(app: FastAPI):
    """Setup MCP server with the FastAPI application"""
    import os
    mcp._mcp_server.name = "mem0-mcp-server"

    # Legacy SSE transport (Claude Code, Python clients)
    app.include_router(mcp_router)

    # Modern streamable-HTTP transport (Claude Desktop, mcp-remote)
    # Mounted OUTSIDE FastAPI's router so it owns the full ASGI lifecycle
    streamable_app = mcp.streamable_http_app()
    wrapped = _StreamableHTTPMiddleware(streamable_app, os.getenv("ADMIN_API_KEY"))
    app.mount("/mcp", wrapped)
