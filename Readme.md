# MCP Server for Kotak Neo Trading

**This repository contains an MCP (Model Context Protocol) server for the Kotak Neo Trading platform, enabling you to trade in natural language through an LLM client such as Claude Desktop.**

**The server acts as a bridge between the MCP client and the Kotak Neo API, providing endpoints to fetch market data, holdings, limits, and execute trades — all in natural language.**

> Fork notice: this is a fork of [BhavyaJethwa/Kotak_Neo_MCP_Server](https://github.com/BhavyaJethwa/Kotak_Neo_MCP_Server) with security fixes applied — see "Changes in this fork" below. This places **real trades with real money** on your live Kotak Neo account. Read the code before you run it.

## 🧰 Tech Stack
1. Python
2. FastAPI
3. redis
4. uvicorn
5. httpx>=0.28.1
6. mcp[cli]>=1.22.0
7. Docker

## ⚙️ MCP Functions Available

The MCP server exposes the following trading operations:
1. `login` — authenticate with TOTP + MPIN, must be called first.
2. `logout` — clear the in-memory session.
3. Get Holdings.
4. Get Limits available.
5. Get current Position.
6. Place a Buy order (requires `confirm=True`).
7. Place a Sell order (requires `confirm=True`).

## Architecture
![Architecture](Kotak_MCP_Server.png)

## ⚠️ Development Issue Encountered

Dependency conflict: the Kotak Neo API client requires `websockets==8.0.0`, while MCP (Model Context Protocol) uses `websockets>=13.x`. These versions are incompatible and cannot coexist in a single Python environment.

## ✅ Resolution

A dedicated, isolated environment was created using Docker: the Kotak Neo API client runs inside a container with `websockets==8.0.0`, and the MCP server (FastAPI) communicates with this worker container over HTTP using `httpx`. This separation ensures both libraries run smoothly without dependency conflicts.

## 🔒 Changes in this fork

The upstream project had several issues that made it unsafe to run against a real account as-is. This fork fixes:

- **Removed a hardcoded session id** that was baked into every tool call in `mcp_server.py`. Session ids are now obtained at runtime via the new `login` tool and kept only in memory for the running process.
- **Added a `login` tool** that actually calls the worker's `/worker/validate/` endpoint (previously unreachable from the MCP tools at all).
- **Added shared-secret authentication** (`WORKER_API_KEY`) on every `/worker/*` endpoint, so a bare session id is no longer enough to place trades if the worker is ever reachable by anyone else.
- **Added optional Redis authentication** (`REDIS_PASSWORD`) — the session store previously had no password.
- **Fixed silent order failures** — `buy`/`sell` used to swallow exceptions and return an empty `200 OK`, making a failed order look like it succeeded (or vice versa). Failures now raise a real error.
- **Added `confirm=True` requirement** on `buy_order`/`sell_order` — an LLM must be explicitly told to confirm before a real market order goes out.
- **Added quantity validation** (must be a positive whole number) on both the MCP tool layer and the worker's request models.
- **Removed unused dependencies** (`langchain`, `langgraph`, `langchain-openai`, `requests`) from `main_api/requirements.txt` — none of it was imported anywhere.
- **Fixed a crash bug** in `main_api/app/api/holdings.py` (`HTTPException(details=...)` → `detail=...`).
- Added `.gitignore` and removed committed `__pycache__`/`.pyc` files.
- Added a `LICENSE` (the upstream repo had none).

This fork has **not** been used to place a real trade — the fixes address what was found in a static code review, not a live end-to-end test. Review the code yourself before connecting a real account.

## Steps to run the MCP server

1. ### 🔨 Building the Docker Image

To build the worker image locally, navigate to the `backend/neo_worker` directory and run:

```bash
docker build -t backend-neo-worker:latest .
```

2. Set required environment variables (generate your own random value for `WORKER_API_KEY`):

```bash
export WORKER_API_KEY="<a long random string you generate yourself>"
export REDIS_PASSWORD="<a separate long random string>"
```

3. Run Redis and the worker image, passing the same secrets through:

```bash
docker run --name redis --network kotak_neo_network -p 127.0.0.1:6379:6379 redis:latest redis-server --requirepass "$REDIS_PASSWORD"
docker run --name neo-worker --network kotak_neo_network -p 127.0.0.1:8001:8001 \
  -e WORKER_API_KEY="$WORKER_API_KEY" -e REDIS_PASSWORD="$REDIS_PASSWORD" \
  backend-neo-worker:latest
```

4. Navigate to root and run, with the same `WORKER_API_KEY` in the environment:

```bash
WORKER_API_KEY="$WORKER_API_KEY" uv run mcp_server.py
```

5. Open Claude Desktop and edit the `claude_desktop_config.json` file, passing the same key through `env`:

```json
{
    "mcpServers": {
        "trade": {
            "command": "/Users/yourname/.local/bin/uv",
            "args": [
                "--directory",
                "/path/to/Kotak_Neo_MCP_Server",
                "run",
                "mcp_server.py"
            ],
            "env": {
                "WORKER_API_KEY": "<the same random string from step 2>"
            }
        }
    }
}
```

6. Restart Claude. It will automatically detect and load the MCP server. Call the `login` tool first with your TOTP, consumer key, mobile number, UCC, and MPIN before using any other tool.

## Links
1. Kotak Neo API : [Kotak Neo API](https://github.com/Kotak-Neo/Kotak-neo-api-v2)
2. MCP official repository : [MCP server python SDK](https://github.com/modelcontextprotocol/python-sdk)
3. Upstream (unforked) project: [BhavyaJethwa/Kotak_Neo_MCP_Server](https://github.com/BhavyaJethwa/Kotak_Neo_MCP_Server)
