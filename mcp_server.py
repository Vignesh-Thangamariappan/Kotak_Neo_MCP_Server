import os
import re
from typing import Optional

from dotenv import load_dotenv
import pyotp
from mcp.server.fastmcp import FastMCP
from fastapi import HTTPException
import httpx
import json

# Loads a local .env file (gitignored) that you create and edit yourself.
# The assistant driving this MCP server never reads this file directly —
# secrets configured here never appear in chat or in tool-call logs.
load_dotenv()

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
async def login(totp: Optional[str] = None):
    """
    Authenticates with Kotak Neo and stores the resulting session id in
    memory for this MCP server process. Must be called before any other
    trading tool.

    Takes NO secret arguments. All credentials are read from environment
    variables loaded from a local .env file that you create and edit
    yourself (see .env.example) — they are never passed through chat or
    tool-call arguments:
      - KOTAK_CONSUMER_KEY
      - KOTAK_MOBILE_NUMBER
      - KOTAK_UCC
      - KOTAK_MPIN
      - KOTAK_TOTP_SECRET (optional: the base32 authenticator-app seed;
        if set, the current 6-digit TOTP code is generated automatically
        and the `totp` argument may be omitted)

    If KOTAK_TOTP_SECRET is not set, pass the current 6-digit code from
    your authenticator app as the `totp` argument instead.
    """
    global _session_id

    def _clean(value: Optional[str]) -> Optional[str]:
        # Common .env footgun: stray surrounding quotes or whitespace end up
        # baked into the value literally. Strip them defensively.
        if value is None:
            return None
        return value.strip().strip('"').strip("'")

    consumer_key = _clean(os.environ.get("KOTAK_CONSUMER_KEY"))
    mobile_number = _clean(os.environ.get("KOTAK_MOBILE_NUMBER"))
    ucc = _clean(os.environ.get("KOTAK_UCC"))
    mpin = _clean(os.environ.get("KOTAK_MPIN"))
    totp_secret = _clean(os.environ.get("KOTAK_TOTP_SECRET"))

    missing = [
        name
        for name, value in [
            ("KOTAK_CONSUMER_KEY", consumer_key),
            ("KOTAK_MOBILE_NUMBER", mobile_number),
            ("KOTAK_UCC", ucc),
            ("KOTAK_MPIN", mpin),
        ]
        if not value
    ]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required env vars in your .env file: {', '.join(missing)}. "
            "See .env.example.",
        )

    # Kotak's API requires the mobile number WITH the ISD/country code, e.g.
    # "+919876543210" (see official docs: mobileNumber "with ISD",
    # example "<+91XXXXXXXXXX>"). Normalize whatever shape the user put in
    # .env into that exact format, rather than assuming a bare 10-digit
    # number is correct.
    digits_only = re.sub(r"[\s-]", "", mobile_number)
    if re.fullmatch(r"\+[0-9]{11,15}", digits_only):
        mobile_number = digits_only
    elif re.fullmatch(r"91[0-9]{10}", digits_only):
        mobile_number = "+" + digits_only
    elif re.fullmatch(r"[0-9]{10}", digits_only):
        mobile_number = "+91" + digits_only
    else:
        is_ascii = True
        try:
            mobile_number.encode("ascii")
        except UnicodeEncodeError:
            is_ascii = False
        raise HTTPException(
            status_code=400,
            detail=(
                "KOTAK_MOBILE_NUMBER could not be normalized to Kotak's expected "
                "+91XXXXXXXXXX format. Enter it as a plain 10-digit number "
                "(e.g. 9876543210) or with country code (+919876543210). "
                f"Current value has length {len(mobile_number)} and is_ascii={is_ascii}. "
                "Check for stray quotes/whitespace/non-ASCII characters in your .env file."
            ),
        )

    if not totp:
        if not totp_secret:
            raise HTTPException(
                status_code=400,
                detail="No `totp` argument given and KOTAK_TOTP_SECRET is not set in .env. "
                "Either set KOTAK_TOTP_SECRET, or pass the current 6-digit code as `totp`.",
            )
        totp = pyotp.TOTP(totp_secret).now()

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


@mcp.tool()
async def get_quotes(instruments: list[dict], quote_type: str = "ltp"):
    """
    Gets live quotes for one or more instruments.

    Parameters:
      - instruments: list of {"exchange_segment": "nse_fo", "instrument_token": "55980"}
        dicts. exchange_segment is one of nse_cm, bse_cm, nse_fo, bse_fo, cde_fo, mcx_fo.
        instrument_token comes from the `tok` field in get_positions/get_holdings output.
      - quote_type: one of "all", "ltp", "ohlc", "depth", "oi", "52W", "circuit_limits",
        "scrip_details". Defaults to "ltp".
    """
    session_id = _require_session()
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{NEO_WORKER_URL}/worker/quotes/{session_id}",
                json={"instruments": instruments, "quote_type": quote_type},
                headers=_auth_headers(),
            )
            response.raise_for_status()
            return response.json()

        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            await _raise_for_worker_error(e)


@mcp.tool()
async def get_order_book():
    """Gets the order book: all orders placed today and their current status."""
    session_id = _require_session()
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{NEO_WORKER_URL}/worker/order-book/{session_id}",
                headers=_auth_headers(),
            )
            response.raise_for_status()
            return response.json()

        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            await _raise_for_worker_error(e)


@mcp.tool()
async def get_order_history(order_id: str):
    """Gets the full status history for a single order id (from get_order_book)."""
    session_id = _require_session()
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{NEO_WORKER_URL}/worker/order-history/{session_id}/{order_id}",
                headers=_auth_headers(),
            )
            response.raise_for_status()
            return response.json()

        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            await _raise_for_worker_error(e)


@mcp.tool()
async def get_trade_book():
    """Gets the trade book: all completed trades for today."""
    session_id = _require_session()
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{NEO_WORKER_URL}/worker/trade-book/{session_id}",
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
