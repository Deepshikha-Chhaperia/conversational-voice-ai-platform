"""
1. ServiceFactory reads the config file and automatically creates the correct STT, LLM, and TTS services. 
2. run_bot() builds and runs the full Pipecat pipeline for each phone call. 
Every call gets its own pipeline, conversation history, and event handlers.
"""

import os
import re
from datetime import datetime
from importlib import import_module
from typing import Any
import httpx

from dotenv import load_dotenv
from loguru import logger

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.frames.frames import (
    TextFrame,
    EndTaskFrame,
    TTSSpeakFrame,
    BotStoppedSpeakingFrame,
    UserStartedSpeakingFrame,
    FunctionCallResultProperties,
    LLMFullResponseEndFrame,
)
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.transports.base_transport import BaseTransport
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.audio.vad.vad_analyzer import VADParams
from fastapi import WebSocket
import json
import asyncio
from services.vobiz_serializer import VobizFrameSerializer

# Load .env file — override=True means .env values take precedence over system environment variables
load_dotenv(override=True)

# Configure logger to file (rotates daily, keeps 10 days)
logger.add("logs/voicebot_{time:YYYY-MM-DD}.log", rotation="1 day", retention="10 days", level="DEBUG")

logger.add("logs/voicebot_{time:YYYY-MM-DD}.log", rotation="1 day", retention="10 days", level="DEBUG")

STREAM_PROVIDER_CALL_IDS: dict[str, str] = {}

# Global VAD Instance
VAD_ANALYZER = None

class ServiceFactory:
    # Dynamic factory for creating AI services from config
    @staticmethod
    def _import_class(class_path: str) -> Any:
        module_path, class_name = class_path.rsplit(".", 1)
        module = import_module(module_path)
        return getattr(module, class_name)

    @classmethod
    def create(cls, service_type: str, provider_name: str, config: dict) -> Any:
        # Creates and returns a ready-to-use STT, LLM, or TTS service using the chosen provider from the config file.
        # Look up the provider in the config registry
        providers = config.get("providers", {}).get(service_type, {})
        provider_config = providers.get(provider_name)

        if not provider_config:
            available = ", ".join(sorted(providers.keys()))
            raise ValueError(
                f"Provider '{provider_name}' not found for {service_type}. "
                f"Available: {available}"
            )

        # Get class path for dynamic import
        class_path = provider_config.get("class_path")
        if not class_path:
            raise ValueError(f"Missing 'class_path' for {service_type}:{provider_name}")

        # Attempt the dynamic import — if the package isn't installed, it will fail
        try:
            service_class = cls._import_class(class_path)
        except (ImportError, AttributeError) as e:
            raise RuntimeError(
                f"Failed to import {class_path}. "
                f"Ensure the package is installed. Error: {e}"
            )

        # kwargs dict for the service constructor
        kwargs = {}

        # Fetch the API key from environment variables.
        api_key_env = provider_config.get("api_key_env")
        if api_key_env:
            api_key = os.getenv(api_key_env)
            if not api_key:
                raise ValueError(f"Missing environment variable: {api_key_env}")
            kwargs["api_key"] = api_key

        # Merge any additional params (model name, base_url, voice_id, etc.)
        params = provider_config.get("params", {})
        kwargs.update(params)

        # creating a service object
        logger.info(f"Creating {service_type} service: {provider_name}")
        return service_class(**kwargs)


def _prune_conversation_history(messages: list, max_messages: int) -> None:
    """
    Keeps the chat history from getting too long for the LLM. It always keeps the first message (the system prompt), 
    removes the oldest conversation turns when the list gets too big, and keeps the most recent messages so the conversation still makes sense. 
    This cleanup runs after each turn to keep the history within safe limits.
    """
    if len(messages) <= max_messages:
        return  # Nothing to prune

    # Keep system prompt (index 0) + the most recent (max_messages - 1) messages
    system_prompt = messages[0]
    recent_messages = messages[-(max_messages - 1):]
    messages.clear()
    messages.append(system_prompt)
    messages.extend(recent_messages)

    logger.debug(f"Pruned conversation history to {len(messages)} messages")


async def run_bot(
    websocket: WebSocket,
    call_type: str,
    config: dict,
    stream_id: str | None = None,
    campaign_data: dict | None = None,
    call_id: str | None = None,
):
    """
    Builds and runs the bot for a single phone call.
    It runs once for each WebSocket connection, meaning every call gets a fresh conversation history so calls never mix with each other.
    """
    provider_call_id = campaign_data.get("provider_call_id") if campaign_data else None

    # If stream_id is missing, parse it from the initial WebSocket message
    if not stream_id:
        try:
            # Wait for the "start" message from Vobiz
            # We might receive multiple messages (e.g., "connected", "ringing"?, then "start")
            # We loop until we find a message with a valid stream_id.
            for attempt in range(5):  # Try 5 messages max as a safeguard
                message = await websocket.receive_text()
                logger.debug(f"WebSocket message {attempt+1}: {message[:200]}...")
                
                data = json.loads(message)
                
                # Check for streamId directly or in 'start' object
                candidate_id = (data.get("streamId") or data.get("start", {}).get("streamId"))
                candidate_call_id = (data.get("callId") or data.get("start", {}).get("callId"))
                if candidate_call_id:
                    provider_call_id = candidate_call_id
                
                if candidate_id:
                    stream_id = candidate_id
                    logger.info(f"[{stream_id}] Found stream_id in message {attempt+1}")
                    break
            
            if not stream_id:
                logger.warning("Could not find stream_id in initial messages, defaulting to 'unknown'")
                stream_id = "unknown"
        except Exception as e:
            logger.error(f"Failed to parse initial WebSocket message: {e}")
            stream_id = "unknown_error"

    logger.info(f"[{stream_id}] Starting {call_type} call (Call ID: {call_id})")

# provider_call_id - Vobiz creates it on THEIR side when they actually initiate the phone call
    if stream_id and provider_call_id:
        STREAM_PROVIDER_CALL_IDS[stream_id] = provider_call_id
    elif stream_id and not provider_call_id:
        provider_call_id = STREAM_PROVIDER_CALL_IDS.get(stream_id)
        if provider_call_id:
            logger.info(f"[{stream_id}] Reusing cached provider callId for hangup fallback")

    async def _force_provider_hangup(trigger: str) -> None:
        if not provider_call_id:
            logger.warning(f"[{stream_id}] Cannot force hangup ({trigger}): missing provider callId")
            return

        auth_id = os.getenv("VOBIZ_AUTH_ID")
        auth_token = os.getenv("VOBIZ_AUTH_TOKEN")
        if not auth_id or not auth_token:
            logger.warning(f"[{stream_id}] Cannot force hangup ({trigger}): missing VOBIZ_AUTH_ID/VOBIZ_AUTH_TOKEN")
            return
        
        # we would need the vobiz internal call id to hang up the call from our side
        hangup_url = f"https://api.vobiz.ai/api/v1/Account/{auth_id}/Call/{provider_call_id}/"
        headers = {
            "X-Auth-ID": auth_id,
            "X-Auth-Token": auth_token,
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.delete(hangup_url, headers=headers)
                if response.status_code in (200, 201, 202, 204):
                    logger.info(f"[{stream_id}] Provider hangup succeeded via API ({trigger})")
                else:
                    logger.warning(f"[{stream_id}] Provider hangup failed: {response.status_code} {response.text}")
        except Exception as e:
            # Swallow connection errors if we are already shutting down
            logger.warning(f"[{stream_id}] Provider hangup exception ({trigger}): {e}")

    # Per-call file logging
    # Create a unique log file for this specific call to isolate logs
    call_log_file = f"logs/call_{call_id}_{stream_id}.log"
    handler_id = logger.add(call_log_file, level="DEBUG")

    # Set up Vobiz serializer
    serializer = VobizFrameSerializer(stream_id=stream_id, sample_rate=8000)

    # Configure VAD
    # Reuse global VAD analyzer if available to save load time (approx 1-2s)
    global VAD_ANALYZER
    if VAD_ANALYZER is None and config.get("vad", {}).get("enabled", True):
        vad_config = config.get("vad", {})
        logger.info("Initializing Global Silero VAD...")
        VAD_ANALYZER = SileroVADAnalyzer(
            params=VADParams(
                start_secs=vad_config.get("min_speech_duration", 0.25),
                stop_secs=vad_config.get("silence_timeout", 0.8),
                confidence=vad_config.get("start_threshold", 0.5)
            )
        )
    
    vad_analyzer = VAD_ANALYZER

    # Create Transport
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            vad_analyzer=vad_analyzer,
            serializer=serializer,
        ),
    )

    # Services are created per-call to ensure thread safety and isolation
    active_providers = config["active_providers"]
    try:
        stt = ServiceFactory.create("stt", active_providers["stt"], config)
        llm = ServiceFactory.create("llm", active_providers["llm"], config)
        tts = ServiceFactory.create("tts", active_providers["tts"], config)
    except Exception as e:
        logger.error(f"[{stream_id}] Failed to create services: {e}")
        return

    # Shared shutdown state to coordinate between tool calls and frame processor
    shutdown_state = {"active": False}

    # Register "end_call" function for LLM to hang up gracefully
    async def end_call_handler(function_name, tool_call_id, args, llm, context, result_callback):
        logger.info(f"[{stream_id}] LLM invoked end_call function. Terminating call.")
        shutdown_state["active"] = True
        await _force_provider_hangup("llm_end_call")
        await llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)
        await result_callback(
            {"status": "ending"},
            properties=FunctionCallResultProperties(run_llm=False),
        )

    llm.register_function("end_call", end_call_handler)

    # Build system prompt using two-layer architecture:
    # Layer 1: Voice rules (always on, compact TTS behavior constraints)
    # Layer 2: Campaign prompt (business logic, changes per campaign) OR base_prompt fallback
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    voice_rules = config.get("voice_rules", "")

    if call_type == "outbound":
        # For outbound: use API-provided campaign prompt -> config campaign_prompt -> base_prompt
        if campaign_data and campaign_data.get("campaign_prompt"):
            campaign_prompt = campaign_data["campaign_prompt"]
        else:
            campaign_prompt = config.get("campaign_prompt", "")

        if campaign_prompt:
            customer_name = campaign_data.get("customer_name", "there") if campaign_data else "there"
            campaign_prompt = campaign_prompt.replace("{customer_name}", customer_name)
            system_prompt = f"{voice_rules}\n\n{campaign_prompt}\n\nCurrent time: {now}"
            logger.info(f"[{stream_id}] Using campaign prompt ({len(campaign_prompt)} chars)")
        else:
            base_prompt = config.get("base_prompt", "You are a helpful AI assistant.")
            system_prompt = f"{voice_rules}\n\n{base_prompt}\n\nCurrent time: {now}"
    else:
        # For inbound: always use base_prompt
        base_prompt = config.get("base_prompt", "You are a helpful AI assistant.")
        system_prompt = f"{voice_rules}\n\n{base_prompt}\n\nCurrent time: {now}"

    # Append HANGUP instruction for ALL calls
    # Modern termination: Signal end of call when goal is met or conversation is over.
    termination_rules = config.get("termination_rules", "")
    if termination_rules:
        system_prompt += f"\n\n{termination_rules}"
    else:
        # Fallback if config is missing rules (should not happen if config.yaml is updated)
        system_prompt += "\n\nIMPORTANT: When the conversation is naturally over, append ' [HANGUP]' to your final response."

    # Each call gets its own message history
    # Starts with just the system prompt and grows as the conversation progresses.
    messages = [{"role": "system", "content": system_prompt}]

    #OpenAILLMContext stores the conversation history and lets Pipecat automatically add what the user says and what the bot replies.
    context = OpenAILLMContext(messages)
    context_aggregator = llm.create_context_aggregator(context)

    
    class TerminationProcessor(FrameProcessor):
        def __init__(self) -> None:
            super().__init__()
            self._termination_requested = False
            self._provider_hangup_sent = False
            self._waiting_for_bot_stop = False
            self._hangup_task = None
            self._buffer = ""  # Buffer for accumulating text frames



            # 1. Silent Triggers: Matches should be STRIPPED and trigger hangup
            self.silent_termination_patterns = [
                # Tool Narration: "Calling the tool", "Invoking end_call"
                ("tool_narration", re.compile(r"\b(call|dial|invok|trigger|activat)\w*\s+(the\s+)?(tool|function|api)\b", re.IGNORECASE)),
                # Explicit Termination commands meant for internal use
                ("explicit_command", re.compile(r"\b(end|clos|terminat|finish|hang)\w*\s+(the\s+)?(call|conversation|session|up|tool)\b", re.IGNORECASE)),
                # Disposition Leaks
                ("disposition_leak", re.compile(r"\b(site\s+visit|inventory|lead)\s+(is\s+)?(booked|sent|partial)\b", re.IGNORECASE)),
                # Explicit Tags
                ("explicit_tag", re.compile(r"\[hangup\]|\[end\]", re.IGNORECASE)),
            ]

            # 2. Spoken Triggers: Matches should be SPOKEN, then trigger hangup
            self.spoken_termination_patterns = [
                # Natural Closing Phrases
                re.compile(r"\b(goodbye|bye|good day|take care)\b", re.IGNORECASE),
                re.compile(r"\bhave a (nice|great|good) (day|evening|night)\b", re.IGNORECASE),
                re.compile(r"\btalk to you (later|soon)\b", re.IGNORECASE),
            ]

        async def _hangup_after_bot_stop(self) -> None:
            await asyncio.sleep(3.0)

            if not self._provider_hangup_sent:
                await _force_provider_hangup("termination_after_bot_stop")
                self._provider_hangup_sent = True

            await self.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)

        async def _schedule_hangup(self, delay: float) -> None:
            await asyncio.sleep(delay)
            if not self._provider_hangup_sent:
                await _force_provider_hangup("termination_immediate")
                self._provider_hangup_sent = True
            await self.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)

        async def process_frame(self, frame, direction: FrameDirection):
            # Gating: Block all downstream speech if shutdown is active
            if shutdown_state["active"] and direction == FrameDirection.DOWNSTREAM:
                if isinstance(frame, (TextFrame, TTSSpeakFrame)):
                     return

            await super().process_frame(frame, direction)

            # Handle Interruption: If user speaks, CANCEL termination
            if direction == FrameDirection.DOWNSTREAM and isinstance(frame, UserStartedSpeakingFrame):
                if self._termination_requested or self._waiting_for_bot_stop:
                    logger.info(f"[{stream_id}] User interrupted termination! Cancelling hangup.")
                    if self._hangup_task:
                        self._hangup_task.cancel()
                        self._hangup_task = None
                    self._waiting_for_bot_stop = False
                    self._termination_requested = False
                    self._provider_hangup_sent = False
                    shutdown_state["active"] = False # Open the gate again
                await self.push_frame(frame, direction)
                return

            if direction == FrameDirection.UPSTREAM and isinstance(frame, BotStoppedSpeakingFrame):
                if self._waiting_for_bot_stop and not self._hangup_task:
                    logger.info(f"[{stream_id}] Bot finished speaking. Hanging up in 3.0s.")
                    self._hangup_task = asyncio.create_task(self._hangup_after_bot_stop())
                    self._waiting_for_bot_stop = False
                await self.push_frame(frame, direction)
                return

            # End of LLM Response -> Flush remaining buffer
            if direction == FrameDirection.DOWNSTREAM and isinstance(frame, LLMFullResponseEndFrame):
                if self._buffer.strip() and not shutdown_state["active"]:
                     # Flush whatever is left in the buffer as a final spoken frame
                     await self.push_frame(TextFrame(text=self._buffer), direction)
                self._buffer = ""
                await self.push_frame(frame, direction)
                return

            if direction == FrameDirection.DOWNSTREAM and isinstance(frame, (TextFrame, TTSSpeakFrame)):
                # 1. Accumulate text
                self._buffer += frame.text
                
                # 2. Analyze Buffer for triggers
                hangup_requested = False
                cleaned_text = self._buffer

                # Check Silent Patterns (Strip them)
                for name, pattern in self.silent_termination_patterns:
                    if pattern.search(cleaned_text):
                        hangup_requested = True
                        logger.info(f"[{stream_id}] Silent Termination Triggered by pattern: '{name}'")
                        cleaned_text = pattern.sub("", cleaned_text) # Remove the trigger phrase
                
                # Check Spoken Patterns (Keep them)
                if not hangup_requested: # Only check if not already triggered by silent custom commands
                    for pattern in self.spoken_termination_patterns:
                        if pattern.search(cleaned_text):
                            hangup_requested = True
                            # Do NOT strip spoken phrases like "Goodbye"
                            break

                if hangup_requested:
                    # Strip leading/trailing punctuation and whitespace
                    cleaned_text = re.sub(r"^[.,!?\s]+|[.,!?\s]+$", "", cleaned_text)
                    
                    logger.info(f"[{stream_id}] Termination Triggered. Buffer: '{self._buffer}'. Cleaned: '{cleaned_text}'")
                    
                    # Update state
                    self._termination_requested = True
                    shutdown_state["active"] = True
                    
                    # Emit any remaining spoken text (e.g. "Goodbye")
                    if cleaned_text.strip():
                        self._waiting_for_bot_stop = True
                        if isinstance(frame, TextFrame):
                            await self.push_frame(TextFrame(text=cleaned_text), direction)
                        elif isinstance(frame, TTSSpeakFrame):
                            await self.push_frame(TTSSpeakFrame(text=cleaned_text), direction)
                    else:
                        # If silent trigger, hang up immediately (don't wait for speaking end)
                        self._waiting_for_bot_stop = False
                        asyncio.create_task(self._schedule_hangup(delay=0.5))
                    
                    # Clear buffer and stop processing further text frames
                    self._buffer = "" 
                    return

                # 3. If safe, check for sentence boundaries to release text
                # Regex looks for [.!?] followed by space or end of string
                if re.search(r"[.!?]\s*$", self._buffer):
                    # We have a full sentence. Release it.
                    text_to_emit = self._buffer
                    self._buffer = ""
                    
                    if isinstance(frame, TextFrame):
                        await self.push_frame(TextFrame(text=text_to_emit), direction)
                    elif isinstance(frame, TTSSpeakFrame):
                        await self.push_frame(TTSSpeakFrame(text=text_to_emit), direction)
                    return
                
                # 4. If no sentence boundary, HOLD the frame (do nothing)
                return

            await self.push_frame(frame, direction)

    termination_processor = TerminationProcessor()

    # Assemble the Pipecat pipeline
    # TTS starts speaking while the LLM is still generating the rest. This keeps Time-to-First-Byte (TTFB) extremely low.
    pipeline = Pipeline([
        transport.input(), # Receives raw audio from Vobiz WebSocket
        stt,  # ASR
        context_aggregator.user(), # Saves users text to conversation history
        llm, # Generates a response based on full context
        termination_processor,
        tts, # Convert the text to audio
        transport.output(), # Send audio back to vobiz - caller hears it
        context_aggregator.assistant() # Save bot's response to conversation history
    ])

    # Pipeline task configuration
    task = PipelineTask(pipeline,params=PipelineParams(
            # Audio sample rates - 8000 Hz is standard telephony
            audio_in_sample_rate=config["audio"]["sample_rate"],
            audio_out_sample_rate=config["audio"]["sample_rate"],

           # If the user starts speaking while the bot is talking, the bot stops speaking, clears any pending audio, and starts listening
            allow_interruptions=config["audio"]["enable_interruptions"],

            # Useful for identifying bottlenecks 
            enable_metrics=config["metrics"]["enable_metrics"],
            enable_usage_metrics=config["metrics"]["enable_usage_metrics"],
        ),
    )

    # Event handlers
    # We keep the system prompt + last 19 exchanges. So that token limit doesnt exceed
    max_messages = config.get("max_conversation_messages", 20)

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        """
        When the call audio starts, the bot sends a greeting first to avoid silence, 
        Gave it as a system message so the LLM knows to speak before the user says anything.
        """
        logger.info(f"[{stream_id}] Call connected ({call_type})")

        # Use campaign greeting if provided, otherwise fall back to config
        if campaign_data and campaign_data.get("greeting"):
            customer_name = campaign_data.get("customer_name", "there")
            greeting = campaign_data["greeting"].replace("{customer_name}", customer_name)
        else:
            greeting_key = f"greeting_{call_type}"
            greeting = config.get(greeting_key, "Say a brief greeting.")

        logger.info(f"[{stream_id}] Sending greeting: {greeting[:100]}...")
        messages.append({"role": "user", "content": greeting})

        # LLM processes the greeting prompt and generates the spoken greeting via TTS.
        await task.queue_frames([context_aggregator.user().get_context_frame()])

        # Safety timeout: hang up after 5 minutes regardless
        async def safety_timeout():
            await asyncio.sleep(300)  # 5 minutes
            logger.warning(f"[{stream_id}] Call exceeded 5 min, forcing hangup")
            await _force_provider_hangup("hard_timeout")
            await task.cancel()
        
        asyncio.create_task(safety_timeout())

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        # When the call ends, the pipeline task is cancelled and the bot shuts down for that call.
        logger.info(f"[{stream_id}] Call disconnected ({call_type})")
        await task.cancel()

    # Run the pipeline
    runner = PipelineRunner(handle_sigint=False) #server does shutdown

    try:
        await runner.run(task)
    except Exception as e:
        # If the pipeline crashes during a call, log the error, keep the server running, and the caller will eventually disconnect after hearing silence.
        logger.error(f"[{stream_id}] Pipeline error during {call_type} call: {e}")
    finally:
        # Remove the per-call log handler
        logger.remove(handler_id)
        
        # Conversation history is cleared after the call ends so old messages do not affect future calls
        _prune_conversation_history(messages, max_messages)
        logger.info(f"[{stream_id}] Pipeline finished ({call_type})")
