import os

from fastapi import FastAPI, HTTPException, Depends, Header
import redis.asyncio as aioredis
import json
from neo_api_client import NeoAPI
from pydantic import BaseModel, Field
import uuid

EIGHTEEN_HOURS_IN_SECONDS = 18 * 60 * 60

WORKER_API_KEY = os.environ.get("WORKER_API_KEY", "")

redis_connection = None


async def verify_api_key(x_worker_api_key: str = Header(default="")):
    """
    Shared-secret check for every /worker/* route. Set WORKER_API_KEY in the
    environment for both this service and the mcp_server.py process to enable
    it. Left unset only for local single-user testing on 127.0.0.1.
    """
    if WORKER_API_KEY and x_worker_api_key != WORKER_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing worker API key.")


def create_redis_client():
    """Returns a client object, but does NOT connect yet."""
    return aioredis.Redis(
        host=os.environ.get("REDIS_HOST", "redis"),
        port=int(os.environ.get("REDIS_PORT", 6379)),
        password=os.environ.get("REDIS_PASSWORD") or None,
        db=0,
        decode_responses=True
    )

async def get_current_client(x_session_id: str):
    
    if not global_redis_client:
        raise HTTPException(status_code=503, detail="Redis service is unavailable.")
    
    # ... (Redis fetch logic) ...
    redis_key = f"session:{x_session_id}"
    session_data_json = await global_redis_client.get(redis_key)
    
    # ... (Error handling) ...

    try:
        session_data = json.loads(session_data_json)
        
        # 1. Initialize Client with the final TRADING_TOKEN
        client = NeoAPI(
            environment=session_data.get("environment"),
            # The TRADING_TOKEN (from totp_validate) is the one required for trading access.
            access_token=session_data.get("TRADING_TOKEN"), 
            neo_fin_key=session_data.get("neo_fin_key"),
            consumer_key=session_data.get("consumer_key"),
        )
        
        # 2. CRITICAL STEP: Manually set the TRADING_SID and BASE_URL
        # The NeoAPI client needs the TRADING_SID/BASE_URL headers for trading endpoints.
        
        # The TRADING_TOKEN from totp_validate is often stored as the internal 'edit_token'
        # The TRADING_SID from totp_validate is often stored as the internal 'edit_sid'
        client.configuration.edit_token = session_data.get("TRADING_TOKEN") 
        client.configuration.edit_sid = session_data.get("TRADING_SID") 
        client.configuration.base_url = session_data.get("BASE_URL")
        
        # 3. Handle potential property name mismatch (if client uses 'bearer_token' internally)
        # Check if the NeoAPI client needs the TRADING_TOKEN stored as 'bearer_token'
        client.configuration.bearer_token = session_data.get("TRADING_TOKEN")
        
        await global_redis_client.expire(redis_key, EIGHTEEN_HOURS_IN_SECONDS)
        
        return client
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to recreate client from session: {e}")
    
class ValidateRequest(BaseModel):
    totp: str = Field(..., min_length=4, max_length=32)
    consumer_key: str
    mobile_number: str
    ucc: str
    mpin: str


# -----------------------------------------------------------

global_redis_client = None

app = FastAPI(title="Koatk Neo Worker", version="1.0.0")

@app.on_event("startup")
async def startup_event():
    global global_redis_client
    try:
        global_redis_client = create_redis_client()
        await global_redis_client.ping()
        print("Connection to Redis success")
    except Exception as e:
        print(f"FATAL: could not connect to redis: {e}")
        global_redis_client = None
        
@app.on_event("shutdown")
async def shutdown_event():
    global global_redis_client
    if global_redis_client:
        await global_redis_client.close()

@app.get("/worker/holdings/{session_id}", dependencies=[Depends(verify_api_key)])
async def get_holdings_data(session_id: str):
    """Fetches holdings using Koatk Neo library (websockets==8.0)."""
    try:
        client = await get_current_client(session_id)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Cannot get client: {e}")
    
    try:
        holdings = client.holdings()
        return {"session_id": session_id, "message": "Holdings fetched", "holdings": holdings}
    except Exception as e:
        # Log the error in the worker service's logs
        print(f"Exception when calling holdings: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching holdings from Koatk Neo: {e}")
    
@app.get("/worker/limits/{session_id}", dependencies=[Depends(verify_api_key)])
async def get_limits_data(session_id: str):
    """Fetches limits using Koatk Neo library (websockets==8.0)."""
    try:
        client = await get_current_client(session_id)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Cannot get client: {e}")
    
    try:
        limits = client.limits()
        return {"session_id": session_id, "message": "Holdings fetched", "limits": limits}
    except Exception as e:
        # Log the error in the worker service's logs
        print(f"Exception when calling limits: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching limits from Koatk Neo: {e}")
    
@app.get("/worker/positions/{session_id}", dependencies=[Depends(verify_api_key)])
async def get_positions_data(session_id: str):
    """Fetches positions using Koatk Neo library (websockets==8.0)."""
    try:
        client = await get_current_client(session_id)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Cannot get client: {e}")
    
    try:
        positions = client.positions()
        return {"session_id": session_id, "message": "Holdings fetched", "positions": positions}
    except Exception as e:
        # Log the error in the worker service's logs
        print(f"Exception when calling positions: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching positions from Koatk Neo: {e}")


class QuoteInstrument(BaseModel):
    exchange_segment: str
    instrument_token: str


class QuotesRequest(BaseModel):
    instruments: list[QuoteInstrument]
    quote_type: str = "ltp"


@app.post("/worker/quotes/{session_id}", dependencies=[Depends(verify_api_key)])
async def get_quotes_data(session_id: str, req: QuotesRequest):
    """Fetches live quotes for the given instruments."""
    try:
        client = await get_current_client(session_id)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Cannot get client: {e}")

    try:
        instrument_tokens = [
            {"exchange_segment": i.exchange_segment, "instrument_token": i.instrument_token}
            for i in req.instruments
        ]
        quotes = client.quotes(instrument_tokens=instrument_tokens, quote_type=req.quote_type)
        return {"session_id": session_id, "message": "Quotes fetched", "quotes": quotes}
    except Exception as e:
        print(f"Exception when calling quotes: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching quotes from Kotak Neo: {e}")


@app.get("/worker/order-book/{session_id}", dependencies=[Depends(verify_api_key)])
async def get_order_book_data(session_id: str):
    """Fetches the current order book (all orders for the day)."""
    try:
        client = await get_current_client(session_id)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Cannot get client: {e}")

    try:
        order_book = client.order_report()
        return {"session_id": session_id, "message": "Order book fetched", "order_book": order_book}
    except Exception as e:
        print(f"Exception when calling order_report: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching order book from Kotak Neo: {e}")


@app.get("/worker/order-history/{session_id}/{order_id}", dependencies=[Depends(verify_api_key)])
async def get_order_history_data(session_id: str, order_id: str):
    """Fetches the status history for a single order id."""
    try:
        client = await get_current_client(session_id)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Cannot get client: {e}")

    try:
        history = client.order_history(order_id=order_id)
        return {"session_id": session_id, "message": "Order history fetched", "history": history}
    except Exception as e:
        print(f"Exception when calling order_history: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching order history from Kotak Neo: {e}")


@app.get("/worker/trade-book/{session_id}", dependencies=[Depends(verify_api_key)])
async def get_trade_book_data(session_id: str):
    """Fetches the trade book (completed trades for the day)."""
    try:
        client = await get_current_client(session_id)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Cannot get client: {e}")

    try:
        trade_book = client.trade_report()
        return {"session_id": session_id, "message": "Trade book fetched", "trade_book": trade_book}
    except Exception as e:
        print(f"Exception when calling trade_report: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching trade book from Kotak Neo: {e}")


from pydantic import BaseModel

class BuyOrderRequest(BaseModel):
    qty: int = Field(..., gt=0)
    stock: str

@app.post("/worker/buy/{session_id}", dependencies=[Depends(verify_api_key)])
async def buy_order(session_id:str, order_data: BuyOrderRequest):
    """ Place order to BUY stock for client """
    try:
        qty = order_data.qty
        qty = str(qty)
        stock = order_data.stock
        client = await get_current_client(session_id)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Cannot get client: {e}")
    try:
        response = client.place_order(
        exchange_segment="nse_cm",
        product="CNC",
        price="0",
        order_type="MKT",
        quantity=qty,
        validity="DAY",
        trading_symbol= stock + "-EQ",
        transaction_type="B",
        amo="YES",
        disclosed_quantity="0",
        market_protection="0",
        pf="N",
        trigger_price="0",
        tag=None,
        scrip_token=None,
        square_off_type=None,
        stop_loss_type=None,
        stop_loss_value=None,
        square_off_value=None,
        last_traded_price=None,
        trailing_stop_loss=None,
        trailing_sl_value=None,
    )
        print(response)
        return response
    except Exception as e:
        print("Exception when calling OrderApi->place_order: %s\n" % e)
        raise HTTPException(status_code=502, detail=f"Order placement failed: {e}")

class SellOrderRequest(BaseModel):
    qty: int = Field(..., gt=0)
    stock: str

@app.post("/worker/sell/{session_id}", dependencies=[Depends(verify_api_key)])
async def sell_order(session_id:str, order_data: SellOrderRequest):
    """ Place order to SELL stock for client """
    try:
        qty = order_data.qty
        qty = str(qty)
        stock = order_data.stock
        client = await get_current_client(session_id)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Cannot get client: {e}")
    try:
        response = client.place_order(
        exchange_segment="nse_cm",
        product="CNC",
        price="0",
        order_type="MKT",
        quantity=qty,
        validity="DAY",
        trading_symbol= stock + "-EQ",
        transaction_type="S",
        amo="YES",
        disclosed_quantity="0",
        market_protection="0",
        pf="N",
        trigger_price="0",
        tag=None,
        scrip_token=None,
        square_off_type=None,
        stop_loss_type=None,
        stop_loss_value=None,
        square_off_value=None,
        last_traded_price=None,
        trailing_stop_loss=None,
        trailing_sl_value=None,
    )
        print(response)
        return response
    except Exception as e:
        print("Exception when calling OrderApi->place_order: %s\n" % e)
        raise HTTPException(status_code=502, detail=f"Order placement failed: {e}")


@app.post("/worker/validate/", dependencies=[Depends(verify_api_key)])
async def validate(req: ValidateRequest):
    if not global_redis_client:
        raise HTTPException(status_code=503, detail="Redis service is unavailable.")
    
    try:
        # 1. Initialize Client (Consumer Key is passed here, but tokens are None)
        client = NeoAPI(
            environment="prod",
            access_token=None,  
            neo_fin_key=None,  
            consumer_key=req.consumer_key,
        )
        
        # --- Step 2a: TOTP Login (Get VIEW_TOKEN) ---
        # NOTE: the underlying neo_api_client library does NOT raise on a
        # failed login/validate call - it only sets client.configuration
        # attributes when Kotak's API returns a 2xx, and otherwise just
        # returns the raw (unraised) error payload. We must inspect these
        # responses ourselves or every failure looks identical downstream.
        login_response = client.totp_login(
            mobile_number=req.mobile_number,
            ucc=req.ucc,
            totp=req.totp
        )

        if not isinstance(login_response, dict) or not login_response.get("data", {}).get("token"):
            raise HTTPException(
                status_code=401,
                detail=f"TOTP login was rejected by Kotak Neo: {login_response}",
            )

        # --- Step 2b: MPIN Validate (Get TRADING_TOKEN) ---
        # The library handles the header construction (Auth, sid) internally based on the view tokens.
        validate_response = client.totp_validate(mpin=req.mpin)

        if not isinstance(validate_response, dict) or not validate_response.get("data", {}).get("token"):
            raise HTTPException(
                status_code=401,
                detail=f"MPIN validation was rejected by Kotak Neo: {validate_response}",
            )

        # --- FINAL TOKEN EXTRACTION FOR REDIS STORAGE ---
        # After totp_validate, the client.configuration should hold the final TRADING tokens.
        config_vars = vars(client.configuration)

        TRADING_TOKEN = config_vars.get('edit_token')  # Assumed to be the TRADING_TOKEN
        TRADING_SID = config_vars.get('edit_sid')      # Assumed to be the TRADING_SID
        BASE_URL = config_vars.get('base_url')

        if not TRADING_TOKEN or not TRADING_SID:
            raise HTTPException(status_code=404,
                                detail="MPIN validation succeeded, but final TRADING tokens (edit_token/edit_sid) were not found.")

        # 2. Store the essential data in Redis
        session_data = {
            # Use final trading tokens for all future API calls
            "TRADING_TOKEN": TRADING_TOKEN,
            "TRADING_SID": TRADING_SID,
            "BASE_URL": BASE_URL,
            
            # Static data
            "consumer_key": req.consumer_key,
            "environment": "prod",
            "neo_fin_key": config_vars.get('neo_fin_key') or "neotradeapi" # Default if not set
        }

        # 3. Serialize and store in Redis with 12-hour expiration
        session_data_json = json.dumps(session_data)
        session_id = str(uuid.uuid4())
        redis_key = f"session:{session_id}"
        
        await global_redis_client.set(redis_key, session_data_json, ex=EIGHTEEN_HOURS_IN_SECONDS)

        return {"session_id": session_id, "message": "Authenticated. Trading session stored in Redis."}
    
    except Exception as e:
        # Provide a general error message, but log the full exception on the server side
        raise HTTPException(status_code=401, detail=f"Authentication failed: {e}")