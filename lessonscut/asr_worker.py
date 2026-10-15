"""Isolated CPU ASR process; also deployable alone on the development server.

Reads a WAV from stdin, emits JSON to stdout. No HTTP listener, CUDA or cloud API.
"""
import hashlib
import io
import json
import sys
import time
from pathlib import Path


def transcribe(audio, model_path, threads=16):
    from faster_whisper import WhisperModel

    started = time.monotonic()
    model = WhisperModel(model_path, device="cpu", compute_type="int8", cpu_threads=threads)
    segments, info = model.transcribe(
        io.BytesIO(audio), language="zh", task="transcribe", beam_size=5,
        word_timestamps=True, vad_filter=True, condition_on_previous_text=False,
        vad_parameters={"min_silence_duration_ms": 500},
    )
    result = []
    for i, segment in enumerate(segments):
        result.append({
            "id": f"asr-{i}", "start": segment.start, "end": segment.end,
            "text": segment.text, "avg_logprob": segment.avg_logprob,
            "no_speech_prob": segment.no_speech_prob,
            "words": [{"start": w.start, "end": w.end, "word": w.word,
                       "probability": w.probability} for w in (segment.words or [])],
        })
    return {"schema_version": "1.0", "engine": "faster-whisper", "device": "cpu",
            "compute_type": "int8", "model": model_path,
            "audio_sha256": hashlib.sha256(audio).hexdigest(),
            "language": info.language, "duration": info.duration,
            "duration_ms": round((time.monotonic() - started) * 1000), "segments": result}


if __name__ == "__main__":
    # Initial isolated provisioning may still be completing an atomic model download.
    # No network fetches or service switches happen in the worker itself.
    deadline=time.monotonic()+600
    while not (Path(sys.argv[1])/"model.bin").exists():
        if time.monotonic()>deadline:raise SystemExit("local ASR model not installed")
        time.sleep(2)
    result = transcribe(sys.stdin.buffer.read(), sys.argv[1], int(sys.argv[2]) if len(sys.argv)>2 else 16)
    print(json.dumps(result, ensure_ascii=False))
