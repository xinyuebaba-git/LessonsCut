"""Local-only transcription adapter, local process or authenticated SSH."""
import hashlib
import json
import shlex
import subprocess
import sys
import fcntl
import re
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .media import run, digest
from .store import Call, uid, encode


class SpeechWord(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    word: str
    probability: float = Field(ge=0, le=1)


class SpeechSegment(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    id: str
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str
    avg_logprob: float
    no_speech_prob: float = Field(ge=0, le=1)
    words: list[SpeechWord]

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start or any(w.end < w.start for w in self.words):
            raise ValueError("invalid ASR timestamp order")
        return self


class Transcript(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    schema_version: str
    engine: str
    device: str
    compute_type: str
    model: str
    audio_sha256: str
    language: str
    duration: float = Field(gt=0)
    duration_ms: int
    segments: list[SpeechSegment]

    @model_validator(mode="after")
    def valid_segments(self):
        if not self.segments or any(s.end > self.duration + 0.5 for s in self.segments) or any(w.end>self.duration+0.5 for s in self.segments for w in s.words):
            raise ValueError("empty or out-of-range ASR result")
        if len({s.id for s in self.segments}) != len(self.segments):
            raise ValueError("duplicate ASR evidence id")
        return self


class LocalASR:
    def __init__(self, settings, store, job_id, revision):
        self.settings, self.store, self.job_id, self.revision = settings, store, job_id, revision

    def transcribe(self, source, work):
        Path(work).mkdir(parents=True,exist_ok=True)
        with (Path(work)/"asr.lock").open("w") as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            return self._transcribe(source,work)

    def _transcribe(self, source, work):
        cfg = self.settings
        if cfg.asr_backend == "disabled":
            raise ValueError("local ASR not configured")
        work = Path(work)
        config = {"source_sha256": digest(source), "backend": cfg.asr_backend,
                  "model": cfg.asr_model_path, "host": cfg.asr_ssh_host,
                  "version": "faster-whisper-1.2.1-zh-words-v1"}
        key = hashlib.sha256(encode(config).encode()).hexdigest()
        cache = work / f"transcript-{key[:16]}.json"
        if cache.exists():
            return Transcript.model_validate_json(cache.read_text())
        audio = work / ("asr-"+config["source_sha256"][:16]+".wav")
        # Existing validated transcript caches return above; new audio always binds to source hash.
        if not audio.exists():
            run(["ffmpeg", "-v", "error", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-n", str(audio)])
        data = audio.read_bytes()
        if cfg.asr_backend == "ssh":
            if not cfg.asr_ssh_host or cfg.asr_ssh_host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:-]+",cfg.asr_ssh_host):
                raise ValueError("invalid ASR SSH host")
            remote = shlex.join([cfg.asr_python, cfg.asr_worker_path, cfg.asr_model_path, str(cfg.asr_threads)])
            args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", cfg.asr_ssh_host, remote]
        else:
            args = [sys.executable, "-m", "lessonscut.asr_worker", cfg.asr_model_path, str(cfg.asr_threads)]
        call_id = uid()
        with self.store.session() as s:
            s.add(Call(id=call_id, job_id=self.job_id, revision=self.revision, role="transcriber",
                       model=cfg.asr_model_path, status="started", details=encode({"usage_status": "missing", "config_hash": key})))
        status = "failed"
        details = {"usage_status": "not_applicable", "usage_kind": "local_audio_seconds", "config_hash": key}
        try:
            p = subprocess.run(args, input=data, capture_output=True, timeout=cfg.asr_timeout_seconds)
            if p.returncode:
                raise ValueError("local ASR process failed: " + p.stderr.decode(errors="replace")[-600:])
            result = Transcript.model_validate_json(p.stdout)
            if result.audio_sha256 != hashlib.sha256(data).hexdigest():
                raise ValueError("ASR audio hash does not match input")
            temp=cache.with_suffix("."+call_id+".tmp")
            temp.write_text(result.model_dump_json(indent=2));temp.replace(cache)
            details.update(duration_ms=result.duration_ms, audio_seconds=result.duration,
                           response_file=str(cache.relative_to(cfg.data_dir)), source_sha256=config["source_sha256"])
            status = "completed"
            return result
        finally:
            with self.store.session() as s:
                call=s.get(Call,call_id);call.status=status;call.details=encode(details)
