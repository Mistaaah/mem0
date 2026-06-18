import httpx
import asyncio
import json
import os

async def run_diag():
    api_key = os.getenv("ADMIN_API_KEY", "")
    if not api_key:
        print("✗ ADMIN_API_KEY not set")
        return

    sse_url = f"https://mem0-api-production-774d.up.railway.app/mcp/vscode/sse/default_user?api_key={api_key}"
    print(f"Targeting: {sse_url[:70]}...")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            print("1. Opening SSE stream...")
            async with client.stream("GET", sse_url) as resp:
                if resp.status_code != 200:
                    print(f"[FAIL] Connection failed: {resp.status_code}")
                    return
                print(f"   [OK] Connected (200 OK)")

                endpoint = None
                post_url = None
                tools_sent = False
                line_count = 0

                async for line in resp.aiter_lines():
                    line_count += 1
                    if not line.strip():
                        continue

                    # Parse endpoint on first event
                    if endpoint is None and line.startswith("data:"):
                        endpoint = line.replace("data:", "").strip()
                        post_url = f"https://mem0-api-production-774d.up.railway.app{endpoint}"
                        print(f"2. Got endpoint from SSE")
                        print(f"3. Sending initialize...")

                        init_payload = {
                            "jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {
                                "protocolVersion": "2024-11-05", "capabilities": {},
                                "clientInfo": {"name": "test", "version": "1.0"}
                            }
                        }
                        init_resp = await client.post(post_url, json=init_payload)
                        if init_resp.status_code >= 400:
                            print(f"   [FAIL] POST failed: {init_resp.status_code}")
                            return
                        print(f"   [OK] Initialize sent (202)")
                        continue

                    # Handle JSON-RPC responses
                    if line.startswith("data:"):
                        try:
                            payload = json.loads(line[5:].strip())
                            if isinstance(payload, dict):
                                msg_id = payload.get("id")

                                if msg_id == 1:
                                    print(f"   [OK] Initialize response received")
                                    print(f"4. Requesting tool list...")

                                    tools_payload = {
                                        "jsonrpc": "2.0", "id": 2, "method": "tools/list",
                                        "params": {}
                                    }
                                    tools_resp = await client.post(post_url, json=tools_payload)
                                    if tools_resp.status_code >= 400:
                                        print(f"   [FAIL] POST failed: {tools_resp.status_code}")
                                        return
                                    print(f"   [OK] Tools request sent")
                                    tools_sent = True

                                elif msg_id == 2 and tools_sent:
                                    tools = payload.get("result", {}).get("tools", [])
                                    print(f"\n[SUCCESS] Found {len(tools)} tools:")
                                    for tool in tools:
                                        name = tool.get("name", "?")
                                        print(f"   - {name}")
                                    print("\n*** MCP SERVER IS WORKING - READY FOR CLAUDE CODE ***")
                                    return
                        except json.JSONDecodeError:
                            pass

                    if line_count > 1000:
                        print("[FAIL] Timeout: no tools response after 1000 lines")
                        return

                print("[FAIL] Stream ended without tools response")

    except asyncio.TimeoutError:
        print("[FAIL] Timeout")
    except Exception as e:
        print(f"[FAIL] Error: {e}")

if __name__ == "__main__":
    asyncio.run(run_diag())
