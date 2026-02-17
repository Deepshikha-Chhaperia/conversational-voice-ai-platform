"""
When the caller speaks, it converts the phone audio into a format the AI can understand (µ-law to PCM) and sends it into the pipeline.
When the bot speaks, it converts the AI's audio back into phone-call format (PCM to µ-law) and sends it to the caller.
"""

import base64
import json
import audioop
from loguru import logger
from pipecat.audio.utils import ulaw_to_pcm, create_stream_resampler
from pipecat.serializers.base_serializer import FrameSerializer
from pipecat.frames.frames import (
    AudioRawFrame,
    Frame,
    InputAudioRawFrame,
    StartFrame,
    StartInterruptionFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
    InputDTMFFrame,
)
from pipecat.audio.dtmf.types import KeypadEntry


class VobizFrameSerializer(FrameSerializer):
    # Acts as an adapter between Vobiz and Pipecat
    def __init__(self, stream_id: str, sample_rate: int = 8000):
        self._stream_id = stream_id  # call identifier
        self._vobiz_sample_rate = sample_rate  # sample rate of vobiz (8000 for telephony)

        # Inside the pipeline, the audio may run at a different rate like 16 kHz if the speech services need it
        self._sample_rate = 0

        # Resamplers handle sample rate conversion so that the audio doesnt sound broken
        self._input_resampler = create_stream_resampler()
        self._output_resampler = create_stream_resampler()

    async def setup(self, frame: StartFrame):
        # Runs once and saves the pipelines preferred audio sample rate so we know how to convert audio correctly
        self._sample_rate = frame.audio_in_sample_rate

    async def serialize(self, frame: Frame) -> str | bytes | None:
        # If the user interrupts, it sends a clearAudio message to stop playback immediately.
        if isinstance(frame, StartInterruptionFrame):
            return json.dumps({"event": "clearAudio", "streamId": self._stream_id})

        # If the bot is speaking, it converts the bots audio into phone-call format and sends it as playAudio
        if isinstance(frame, AudioRawFrame):
            # Resample to 8000Hz if needed
            if frame.sample_rate != self._vobiz_sample_rate:
                audio_data = await self._output_resampler.resample(
                    frame.audio,
                    frame.sample_rate,
                    self._vobiz_sample_rate
                )
            else:
                audio_data = frame.audio

            if not audio_data:
                logger.warning("Serialized audio data is empty")
                return None

            # Convert PCM (16-bit) to Mu-Law (8-bit)
            # audio_data is 16-bit linear PCM at 8000Hz (ensured by resampler check above)
            try:
                ulaw_data = audioop.lin2ulaw(audio_data, 2)
            except Exception as e:
                logger.error(f"Error encoding audio to mu-law: {e}")
                return None

            payload = base64.b64encode(ulaw_data).decode("utf-8")
            
            # Log periodically to avoid spam, or just log size
            # logger.debug(f"Sending audio frame: {len(payload)} bytes")
            
            return json.dumps(
                {
                    "event": "playAudio",
                    "streamId": self._stream_id,
                    "media": {
                        "contentType": "audio/x-mulaw",
                        "sampleRate": 8000,
                        "payload": payload,
                    },
                }
            )

        # If it is a custom event, it simply passes the JSON message (e.g call transfer)
        if isinstance(frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)):
            return json.dumps(frame.message)
        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        # Convert a Vobiz WebSocket message into formats the Pipecat pipeline can understand.
        try:
            message = json.loads(data)
        except json.JSONDecodeError:
            logger.warning("Failed to parse WebSocket JSON payload")
            return None

        # Incoming audio from the caller, Vobiz sends that voice data to server
        if message.get("event") == "media":
            media = message.get("media", {})
            payload_base64 = media.get("payload")
            if not payload_base64:
                return None

            # The audio arrives as encoded text. We convert that text back into real audio bytes.
            payload = base64.b64decode(payload_base64)

            # We check what type of audio Vobiz sent, either µ-law (phone format) or raw PCM
            content_type = str(media.get("contentType", "audio/x-mulaw")).lower()
            input_rate = int(media.get("sampleRate", self._vobiz_sample_rate))

            if "audio/x-l16" in content_type:
                # If the audio is in PCM format, we only adjust the sample rate if it does not match the pipeline
                if input_rate == self._sample_rate:
                    deserialized_data = payload
                else:
                    deserialized_data = await self._input_resampler.resample(payload, input_rate, self._sample_rate)
            else:
                # If the audio is in phone format, we convert it into normal PCM and adjust the sample rate so the AI can use it
                deserialized_data = await ulaw_to_pcm(payload, input_rate, self._sample_rate, self._input_resampler)

            if not deserialized_data:
                return None

            # We package the converted audio into a format the Pipecat pipeline understands and send it to the bot
            return InputAudioRawFrame(audio=deserialized_data, num_channels=1, sample_rate=self._sample_rate)

        # If the caller presses a number key on the phone, we detect and send it into the pipeline instead of speech
        if message.get("event") == "dtmf":
            digit = message.get("dtmf", {}).get("digit") or message.get("digit")
            if not digit:
                return None
            try:
                return InputDTMFFrame(KeypadEntry(digit))
            except ValueError:
                logger.warning(f"Invalid DTMF digit received: {digit}")

        return None
