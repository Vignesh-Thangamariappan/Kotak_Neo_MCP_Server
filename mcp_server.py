import os
from typing import Optional

from mcp.server.fastmcp import FastMCP
from fastapi import HTTPException
import httpx
import json

mcp = FastMCP("Kotak-MCP-Server")

NEO_WORKER_URL = os.environ.get("NEO_WORKER_URL", "http://127.0.0.1:8001")
WORKER_API_KEY = os.environ.get("WORKER_API_KEY", "")

# Session id is obtained via the `login` tool at runtime and kept in memory
# for this process only. It is never hardcoded and never persisted to disk.
_session_id: Optional[str] = None


def _auth_headers() -> dict:
    return {"X-Worker-Api-Key": WORKER_API_KEY} if WORKER_API_KEY else {}


def _require_session() -> str:
    if not _session_id:
        raise HTTPException(
            status_code=401,
            detail="Not logged in. Call the `login` tool first with your TOTP, "
            "consumer_key, mobile_number, ucc, and mpin.",
        )
    return _session_id


async def _raise_for_worker_error(e: Exception):
    if isinstance(e, httpx.HTTPStatusError):
        error_detail = "unknown error"
        try:
            error_detail = e.response.json().get("detail", "unknown error")
        except Exception:
            error_detail = str(e)
        raise HTTPException(
            status_code=e.response.status_code,
            detail=f"worker error: {error_detail}",
        )
    if isinstance(e, httpx.RequestError):
        raise HTTPException(
            status_code=503, detail=f"Cannot connect to Neo Worker service: {e}"
        )
    raise


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers"""
    return a + b


@mcp.tool()
async def login(totp: str, consumer_key: str, mobile_number: str, ucc: str, mpin: str):
    """
    Authenticates with Kotak Neo using TOTP + MPIN and stores the resulting
    session id in memory for this MCP server process. Must be called before
    any other trading tool. Credentials are sent once to the local worker
    service and are never logged or persisted by this tool.
    """
    global _session_id
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{NEO_WORKER_URL}/worker/validate/",
                json={
                    "totp": totp,
                    "consumer_key": consumer_key,
                    "mobile_number": mobile_number,
                    "ucc": ucc,
                    "mpin": mpin,
                },
                headers=_auth_headers(),
            )
            response.raise_for_status()
            data = response.json()
            _session_id = data.get("session_id")
            return {"message": "Login successful. Session is active for this MCP server process."}
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            await _raise_for_worker_error(e)


@mcp.tool()
def logout():
    """Clears the in-memory trading session for this MCP server process."""
    global _session_id
    _session_id = None
    return {"message": "Logged out."}


@mcp.tool()
async def get_holdings():
    """Gets the current holdings of the logged-in client."""
    session_id = _require_session()
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{NEO_WORKER_URL}/worker/holdings/{session_id}",
                headers=_auth_headers(),
            )
            response.raise_for_status()
            response = response.json()
            required_keys = [
                "instrumentName", "quantity", "averagePrice",
                "holdingCost", "closingPrice", "unrealisedGainLoss",
            ]

            output = {
                "message": response.get("message", ""),
                "holdings": [],
            }

            for item in response.get("holdings", {}).get("data", []):
                filtered = {key: item.get(key) for key in required_keys}
                output["holdings"].append(filtered)

            return json.dumps(output)

        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            await _raise_for_worker_error(e)


@mcp.tool()
async def get_limits():
    """Gets the limits of the logged-in client."""
    session_id = _require_session()
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{NEO_WORKER_URL}/worker/limits/{session_id}",
                headers=_auth_headers(),
            )
            response.raise_for_status()
            return response.json()

        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            await _raise_for_worker_error(e)


@mcp.tool()
async def get_positions():
    """Gets the positions of the logged-in client."""
    session_id = _require_session()
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{NEO_WORKER_URL}/worker/positions/{session_id}",
                headers=_auth_headers(),
            )
            response.raise_for_status()
            return response.json()

        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            await _raise_for_worker_error(e)


def _validate_qty(qty: str) -> str:
    try:
        qty_int = int(qty)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="qty must be a whole number.")
    if qty_int <= 0:
        raise HTTPException(status_code=400, detail="qty must be greater than 0.")
    return str(qty_int)


@mcp.tool()
async def buy_order(qty: str, stock: str, confirm: bool = False):
    """
    Places a MARKET BUY order for the logged-in client via the local worker service.

    Parameters:
      - qty: whole number of shares (>0)
      - stock: e.g. "SUZLON", "IDEA", "GRSE", "HAL", "BDL" (always upper-case)
      - confirm: must be explicitly set to True. This places a real market
        order with real money; it will NOT run unless confirm=True.
    """
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="This places a real market order with real money. "
            "Re-call this tool with confirm=True after the user has explicitly confirmed.",
        )

    session_id = _require_session()
    qty = _validate_qty(qty)
    payload = {"qty": qty, "stock": stock.upper()}

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{NEO_WORKER_URL}/worker/buy/{session_id}",
                json=payload,
                headers=_auth_headers(),
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            await _raise_for_worker_error(e)


@mcp.tool()
async def sell_order(qty: str, stock: str, confirm: bool = False):
    """
    Places a MARKET SELL order for the logged-in client via the local worker service.

    Parameters:
      - qty: whole number of shares (>0)
      - stock: e.g. "SUZLON", "IDEA", "GRSE", "HAL", "BDL" (always upper-case)
      - confirm: must be explicitly set to True. This places a real market
        order with real money; it will NOT run unless confirm=True.
    """
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="This places a real market order with real money. "
            "Re-call this tool with confirm=True after the user has explicitly confirmed.",
        )

    session_id = _require_session()
    qty = _validate_qty(qty)
    payload = {"qty": qty, "stock": stock.upper()}

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{NEO_WORKER_URL}/worker/sell/{session_id}",
                json=payload,
                headers=_auth_headers(),
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            await _raise_for_worker_error(e)


def main():
    # Initialize and run the server
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
