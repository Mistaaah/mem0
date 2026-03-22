import httpx
import asyncio
import json
import sys

async def run_diag():
    url = "http://127.0.0.1:8000/mcp/antigravity/sse/gonzo"
    headers = {
        "X-API-Key": "x@!b2BFg&zFEnK%!3ekK",
        "Content-Type": "application/json"
    }
    
    print(f"Targeting MCP Server at: {url}")
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            print("1. Establishing SSE connection...")
            async with client.stream("GET", url, headers=headers) as response:
                print(f"SSE Status: {response.status_code}")
                if response.status_code != 200:
                    print(f"Failed to connect: {response.status_code}")
                    return
 
                print("2. Discovery - waiting for endpoint event...")
                endpoint = None
                async for line in response.aiter_lines():
                    print(f"DEBUG: {line}")
                    if line.startswith("data:"):
                        endpoint = line[5:].strip()
                        print(f"Endpoint received: {endpoint}")
                        break
                
                if not endpoint:
                    print("No endpoint received.")
                    return
                
                post_url = "https://mem0-production-6969.up.railway.app" + endpoint
                
                print(f"3. Sending 'initialize' request to {post_url}...")
                init_payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "Antigravity-Diag", "version": "1.0.0"}
                    }
                }
                
                post_resp = await client.post(post_url, json=init_payload, headers=headers)
                print(f"POST Result: {post_resp.status_code}")
                if post_resp.status_code >= 400:
                    print(f"POST Body: {post_resp.text}")
                    return

                print("4. Waiting for JSON-RPC response in SSE stream...")
                async for line in response.aiter_lines():
                    print(f"DEBUG SSE: {line}")
                    if line.startswith("data:"):
                        payload = json.loads(line[5:].strip())
                        if payload.get("id") == 1:
                            print("SUCCESS: Initialize complete!")
                            
                            print("5. Requesting tool list...")
                            tools_payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
                            tools_resp = await client.post(post_url, json=tools_payload, headers=headers)
                            print(f"Tools POST Status: {tools_resp.status_code}")
                            
                            async for t_line in response.aiter_lines():
                                if t_line.startswith("data:"):
                                    t_payload = json.loads(t_line[5:].strip())
                                    if t_payload.get("id") == 2:
                                        tools = t_payload.get("result", {}).get("tools", [])
                                        print(f"SUCCESS: Found {len(tools)} tools.")
                                        for t in tools:
                                            print(f" - {t['name']}")
                                        print("\n*** DIAGNOSTIC PASSED: THE SERVER IS WORKING PERFECTLY ***")
                                        return
                            break
    except Exception as e:
        print(f"Error during diagnostic: {e}")

if __name__ == "__main__":
    asyncio.run(run_diag())
