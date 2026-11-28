"""Read-only diagnostic of Ryanair public MCP server. Never changes calendars."""
import json
import sys
import urllib.request
import urllib.error

URL = "https://services-api.ryanair.com/mcp-server/mcp"
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "User-Agent": "ryanair-calendar-probe/2.0"}

def rpc(method, params=None, session=None, request_id=1):
    body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}).encode()
    headers = dict(HEADERS)
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(URL, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as response:
        text = response.read().decode("utf-8", "replace")
        session = response.headers.get("Mcp-Session-Id", session)
        content_type = response.headers.get("Content-Type", "")
        if "text/event-stream" in content_type:
            payloads = [line[6:].strip() for line in text.splitlines() if line.startswith("data:")]
            objects = [json.loads(x) for x in payloads if x and x != "[DONE]"]
            data = next((x for x in objects if x.get("id") == request_id), objects[-1] if objects else {})
        else:
            data = json.loads(text)
        return data, session

def main():
    print("Read-only Ryanair MCP connection test. No calendar files modified.")
    try:
        init, session = rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "ryanair-calendar-probe", "version": "2.0"}})
        if "error" in init:
            print("Initialize error:", json.dumps(init["error"], ensure_ascii=False))
            return 1
        print("Initialize OK; server:", json.dumps(init.get("result", {}).get("serverInfo", {}), ensure_ascii=False))
        tools, _ = rpc("tools/list", session=session, request_id=2)
        if "error" in tools:
            print("Tool list error:", json.dumps(tools["error"], ensure_ascii=False))
            return 1
        names = [t.get("name") for t in tools.get("result", {}).get("tools", [])]
        print("Available tools:", json.dumps(names, ensure_ascii=False))
        print("NOTE: Connection/tool discovery only. No flight completeness verified.")
        return 0
    except urllib.error.HTTPError as e:
        print("HTTP error:", e.code, e.read().decode("utf-8", "replace")[:1000])
    except Exception as e:
        print("Connection error:", type(e).__name__, str(e))
    return 1

if __name__ == "__main__":
    sys.exit(main())
