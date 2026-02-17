import asyncio
import re
import uuid
from fastapi import FastAPI, WebSocket, HTTPException, Request, Header
from fastapi.responses import Response
from pydantic import BaseModel, field_validator
import httpx
import yaml
import os
import uvicorn
from loguru import logger
from contextlib import asynccontextmanager

from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from bot import run_bot

CONFIG = None

# Pending campaign data for outbound calls, keyed by call_id (UUID).
# Stored when /outbound is hit, consumed when the WebSocket connects.
# In production with multiple servers, this would move to Redis.
PENDING_CALLS: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Initialize AI services once at startup, tear down on shutdown.
    This runs BEFORE the first request is served. If any service fails to
    initialize (bad API key, missing package), the server won't start.
    """
    global CONFIG, SERVICES

    logger.info("Initializing voice bot services...")

    # Load the config that defines which providers are active
    with open("config.yaml", "r") as f:
        CONFIG = yaml.safe_load(f)

    # Load the config that defines which providers are active
    with open("config.yaml", "r") as f:
        CONFIG = yaml.safe_load(f)

    logger.info("Voice bot config loaded.")
    yield
    logger.info("Shutting down...")
app = FastAPI(title="Plug-and-Play Voice Bot", lifespan=lifespan)


# INBOUND CALL HANDLER
@app.post("/inbound")
async def handle_inbound_call(request: Request):
    """
    When someone calls our Vobiz phone number, Vobiz sends a POST request.
    We respond with VXML (XML instructions) telling Vobiz to Open a bidirectional audio stream to our WebSocket endpoint.

    The audio format is µ-law at 8kHz — the standard for telephone networks
    VobizFrameSerializer handles the conversion to PCM that Pipecat expects.
    """
    # Determine the WebSocket URL from the incoming request's host header.
    host = request.headers.get("host")
    scheme = "wss" if request.url.scheme == "https" else "ws"
    stream_url = f"{scheme}://{host}/ws"

    # VXML instructs Vobiz to open a bidirectional µ-law audio stream.
    # "bidirectional=true" means server receive the caller's audio AND send bot's audio back through the same stream.
    # keepCallAlive="true" keeps the live call active across short stream hiccups.
    # audioTrack="inbound" is required by the reference implementation.
    vxml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream bidirectional="true" audioTrack="inbound" contentType="audio/x-mulaw;rate=8000" keepCallAlive="true">{stream_url}</Stream>
</Response>"""

    logger.info(f"Inbound call directed to {stream_url}")
    return Response(content=vxml, media_type="application/xml")


# OUTBOUND CALL HANDLER
# This prevents garbage strings from hitting the Vobiz API.
INDIAN_PHONE_PATTERN = re.compile(r"^\+91[6-9]\d{9}$")


class OutboundCallRequest(BaseModel):
    to: str  # Phone number to call
    customer_name: str = "there"  # Name for greeting personalization
    campaign_prompt: str | None = None  # Override campaign prompt (if None, uses config.yaml)
    greeting: str | None = None  # Override greeting (if None, uses config.yaml)

    @field_validator("to")
    @classmethod
    def validate_phone_number(cls, v: str) -> str:
        """Validate that 'to' is a valid Indian mobile number."""
        if not INDIAN_PHONE_PATTERN.match(v):
            raise ValueError(
                f"Invalid phone number format: '{v}'. "
                f"Must be an Indian number"
            )
        return v


@app.post("/outbound")
async def start_outbound_call(
    request: Request,
    body: OutboundCallRequest,
    x_api_key: str | None = Header(None)
):
    """
    Initiate an outbound call via the Vobiz REST API.

    Flow:
      1. Caller hits POST /outbound with {"to": "+1234567890"}
      2. We call Vobiz API to initiate the phone call
      3. When the callee picks up, Vobiz hits our /outbound-answer endpoint
      4. /outbound-answer responds with VXML pointing to /ws (same as inbound)
      5. Audio streaming begins through the same pipeline

    The 'to' number comes from the caller and 'from' is our Vobiz number.
    """
    # API KEY AUTHENTICATION
    required_key = os.getenv("OUTBOUND_API_KEY")
    if not required_key:
        logger.warning("OUTBOUND_API_KEY not set in environment! Allowing request (RISKY).")
    elif x_api_key != required_key:
        logger.warning(f"Unauthorized outbound call attempt. Key provided: {x_api_key}")
        raise HTTPException(status_code=401, detail="Invalid API Key")

    logger.info("OUTBOUND CALL REQUEST RECEIVED")
    logger.info(f"Target number: {body.to}, customer: {body.customer_name}")

    # Pull Vobiz credentials from environment variables.
    auth_id = os.getenv("VOBIZ_AUTH_ID")
    auth_token = os.getenv("VOBIZ_AUTH_TOKEN")
    from_number = os.getenv("VOBIZ_FROM_NUMBER")

    if not all([auth_id, auth_token, from_number]):
        logger.error("Missing Vobiz credentials in environment")
        raise HTTPException(400, "Missing Vobiz credentials in environment")

    logger.info("Vobiz credentials loaded from environment")

    # Store campaign data so it can be retrieved when the WebSocket connects.
    call_id = str(uuid.uuid4())
    PENDING_CALLS[call_id] = {
        "customer_name": body.customer_name,
        "campaign_prompt": body.campaign_prompt,
        "greeting": body.greeting,
    }
    logger.info(f"[{call_id}] Campaign data stored for outbound call")

    # Build the answer_url dynamically from the current request host.
    # call_id is passed as query param so /outbound-answer can forward it to the WS.
    host = request.headers.get("host")
    scheme = "https" if request.url.scheme == "https" else "http"
    answer_url = f"{scheme}://{host}/outbound-answer?call_id={call_id}"

    # Vobiz REST API call to initiate the phone call
    vobiz_url = f"https://api.vobiz.ai/api/v1/Account/{auth_id}/Call/"
    payload = {
        "from": from_number,
        "to": body.to,
        "answer_url": answer_url,
        "answer_method": "POST"
    }
    headers = {
        "X-Auth-ID": auth_id,
        "X-Auth-Token": auth_token,
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.post(vobiz_url, json=payload, headers=headers)
            response.raise_for_status()
            result = response.json()
            provider_call_id = result.get("callId") or result.get("call_uuid") or result.get("uuid")
            if provider_call_id:
                PENDING_CALLS[call_id]["provider_call_id"] = provider_call_id
            logger.info(f"Outbound call initiated to {body.to}, answer_url={answer_url}")
            return {"status": "success", "vobiz_response": result}
        except httpx.HTTPStatusError as e:
            # Log the upstream response body so we can see the exact Vobiz validation error.
            error_body = e.response.text
            logger.error(f"Vobiz API error: {e} | Response: {error_body}")
            raise HTTPException(500, f"Failed to initiate outbound call. Vobiz error: {error_body}")
        except httpx.HTTPError as e:
            logger.error(f"Vobiz API error: {e}")
            raise HTTPException(500, "Failed to initiate outbound call. Check server logs.")


# OUTBOUND ANSWER CALLBACK
@app.post("/outbound-answer")
async def handle_outbound_answer(request: Request):
    """
    Vobiz callback when the outbound callee picks up.
    Identical to /inbound - we return VXML that tells Vobiz to open a bidirectional audio stream to /ws. The only difference
    is the ?type=outbound query param so the bot knows to use the outbound greeting instead of the inbound one.
    """
    host = request.headers.get("host")
    scheme = "wss" if request.url.scheme == "https" else "ws"
    call_id = request.query_params.get("call_id", "")
    stream_url = f"{scheme}://{host}/ws?type=outbound&amp;call_id={call_id}"

    vxml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream bidirectional="true" audioTrack="inbound" contentType="audio/x-mulaw;rate=8000" keepCallAlive="true">{stream_url}</Stream>
</Response>"""

    logger.info(f"Outbound call answered, directed to {stream_url}")
    return Response(content=vxml, media_type="application/xml")

# WEBSOCKET (audio pipeline)
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    Unified WebSocket handler for both inbound and outbound calls.

    The full flow is:
      1. Vobiz connects and sends a "start" message with stream metadata
      2. Set up the Pipecat transport with Vobiz-specific serialization (µ-law -> PCM)
      3. Voice Activity Detection (VAD) for when the human is speaking vs silent
      4. Bot pipeline: Audio In -> ASR -> LLM -> TTS -> Audio Out
      5. When the call ends, Vobiz closes the WebSocket

    The call is wrapped in asyncio.wait_for() to enforce a maximum duration.
    This prevents stuck WebSocket connections from consuming resources.
    """
    await websocket.accept()
    logger.info("WebSocket accepted")

    # Determine if this is an inbound or outbound call from the query string.
    call_type = websocket.query_params.get("type", "inbound")
    logger.info(f"Call type: {call_type}")

    # Track stream_id for logging — initialized to "unknown" in case first message parsing fails.
    # We pass None to allow bot.py to handle handshake.
    stream_id = None

    try:
        # 1. Do NOT manually read the first message. Let the pipeline handle it.
        # This prevents consuming the "start" message which Pipecat needs.
        logger.info(f"WebSocket connected ({call_type})")

        # 2. Retrieve campaign data for outbound calls (pop removes it from the store)
        call_id = websocket.query_params.get("call_id", "")
        campaign_data = PENDING_CALLS.pop(call_id, None) if call_id else None
        if campaign_data:
            logger.info(f"Campaign data loaded for call {call_id}")

        # 3. Run the bot pipeline with a maximum call duration so calls automatically stop if they run too long (default 15 minutes).
        max_duration = CONFIG.get("max_call_duration_seconds", 900)

        await asyncio.wait_for(
            run_bot(
                websocket=websocket,
                call_type=call_type,
                config=CONFIG,
                stream_id=stream_id,
                campaign_data=campaign_data,
                call_id=call_id,
            ),
            timeout=max_duration,
        )

    except asyncio.TimeoutError:
        logger.warning(f"[{stream_id}] Call exceeded max duration, terminating")
    except Exception as e:
        logger.error(f"[{stream_id}] WebSocket error ({call_type}): {e}")
    finally:
        logger.info(f"[{stream_id}] WebSocket closed ({call_type})")


# HEALTH CHECK
@app.get("/health")
async def health():
    # Health check endpoint reports which services are configured
    active = CONFIG.get("active_providers", {})
    return {"status": "healthy", "configured_services": active}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
