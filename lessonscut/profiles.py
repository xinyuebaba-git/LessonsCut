"""Versioned role configuration; credentials live only in owner-readable files.

Jobs reference immutable versions, never credentials or mutable environment state.
Changing endpoints does not install, start or switch any GPU service.
"""
import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from .config import Settings
from .store import ConfigurationHead, ConfigurationVersion, encode, uid

ROLES = ("recognizer", "supervisor", "image")
KEY_FIELDS = {"recognizer": "qwen_api_key", "supervisor": "minimax_api_key", "image": "dashscope_api_key"}
ENDPOINT_FIELDS = {"recognizer": "qwen_base_url", "supervisor": "minimax_base_url", "image": "dashscope_base_url"}
MODEL_FIELDS = {"recognizer": "qwen_model", "supervisor": "minimax_supervisor_model", "image": "qwen_image_model"}


class RoleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    provider: str = Field(min_length=1, max_length=80)
    protocol: Literal["openai", "minimax", "dashscope"]
    url: str = Field(max_length=1000)
    model: str = Field(min_length=1, max_length=160)
    api_key: SecretStr | None = None
    clear_key: bool = False
    timeout: int = Field(ge=10, le=1800)
    limit: int = Field(ge=1, le=500)

    @field_validator("url")
    @classmethod
    def endpoint(cls, value):
        u = urlsplit(value)
        try:
            port = u.port
        except ValueError:
            raise ValueError("invalid endpoint port") from None
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment or any(c.isspace() for c in value):
            raise ValueError("HTTP(S) endpoint without credentials/query/fragment required")
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("invalid endpoint port")
        return value.rstrip("/")

    @field_validator("model", "provider")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("nonblank value required")
        return value.strip()


class TemplateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intro_asset_id: str | None = None
    fallback_style: bool = True
    closing_hold: bool = True


class ProfileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_version: int = Field(ge=1)
    roles: dict[str, RoleConfig]
    template: TemplateConfig
    max_calls_per_job: int = Field(default=160, ge=1, le=500)


class ProfileConflict(ValueError):
    pass


class Profiles:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.vault = settings.data_dir / "credentials"

    def put_secret(self, value):
        self.vault.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.vault, 0o700)
        name = uid()
        with os.fdopen(os.open(self.vault / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            f.write(value)
        return name

    def secret(self, reference):
        if not reference:
            return ""
        # Stored references are generated UUID filenames, never arbitrary paths.
        if Path(reference).name != reference or len(reference) != 36:
            raise ValueError("invalid credential reference")
        return (self.vault / reference).read_text()

    def bootstrap(self):
        with self.store.session() as s:
            head = s.get(ConfigurationHead, 1)
            if head:
                return head.version
        cfg = self.settings
        providers = ("本地 OpenAI 兼容服务", "MiniMax 官方", "阿里云百炼")
        protocols = (cfg.recognizer_protocol, cfg.supervisor_protocol, "dashscope")
        roles = {}
        for i, role in enumerate(ROLES):
            key = getattr(cfg, KEY_FIELDS[role]).get_secret_value()
            roles[role] = {"provider": providers[i], "protocol": protocols[i], "url": getattr(cfg, ENDPOINT_FIELDS[role]),
                           "model": getattr(cfg, MODEL_FIELDS[role]), "timeout": cfg.role_timeouts.get(role, 600 if role == "image" else 300),
                           "limit": cfg.role_call_limits.get(role, 3 if role == "image" else 160),
                           "credential_ref": self.put_secret(key) if key else None}
        data = {"roles": roles, "template": {"intro_asset_id": cfg.default_intro_asset_id,
                "fallback_style": cfg.title_style_fallback, "closing_hold": cfg.ending_policy == "freeze_for_closing_speech"},
                "max_calls_per_job": cfg.max_calls_per_job}
        try:
            with self.store.session() as s:
                s.add(ConfigurationVersion(version=1, data=encode(data)))
                s.add(ConfigurationHead(id=1, version=1))
        except IntegrityError:
            # API and worker may concurrently discover a legacy database.
            with self.store.session() as s:
                return s.get(ConfigurationHead, 1).version
        return 1

    def raw(self, version=None):
        if version is None:
            self.bootstrap()
        with self.store.session() as s:
            version = version or s.get(ConfigurationHead, 1).version
            row = s.get(ConfigurationVersion, version)
            if row is None:
                raise ValueError("configuration version missing")
            return version, json.loads(row.data)

    def public(self, version=None):
        version, data = self.raw(version)
        for role in ROLES:
            item = data["roles"][role]
            reference = item.pop("credential_ref", None)
            item["key_configured"] = bool(reference and (self.vault / reference).is_file())
        return {"version": version, **data}

    def save(self, body):
        if set(body.roles) != set(ROLES):
            raise ValueError("exactly three model roles required")
        for role, config in body.roles.items():
            supported = {"recognizer": {"openai"}, "supervisor": {"openai", "minimax"}, "image": {"dashscope"}}
            if config.protocol not in supported[role]:
                raise ValueError("unsupported protocol for " + role)
        version, previous = self.raw()
        if version != body.base_version:
            raise ProfileConflict("configuration changed; reload before saving")
        data = {"roles": {}, "template": body.template.model_dump(), "max_calls_per_job": body.max_calls_per_job}
        new_refs = []
        try:
            for role, config in body.roles.items():
                item = config.model_dump(exclude={"api_key", "clear_key"})
                key = config.api_key.get_secret_value() if config.api_key else ""
                if key and config.clear_key:
                    raise ValueError('cannot replace and clear the same credential together')
                reference = previous["roles"][role].get("credential_ref")
                # Never silently send an existing secret to a newly configured endpoint.
                if reference and item["url"] != previous["roles"][role]["url"] and not key and not config.clear_key:
                    raise ValueError("endpoint changed: enter a key for the new endpoint, or explicitly clear the existing key")
                if key:
                    reference = self.put_secret(key); new_refs.append(reference)
                elif config.clear_key:
                    reference = None
                item["credential_ref"] = reference
                data["roles"][role] = item
            with self.store.session() as s:
                changed = s.execute(update(ConfigurationHead).where(ConfigurationHead.id == 1, ConfigurationHead.version == version).values(version=version + 1)).rowcount
                if changed != 1:
                    raise ProfileConflict("configuration changed; reload before saving")
                s.add(ConfigurationVersion(version=version + 1, data=encode(data)))
        except Exception:
            for reference in new_refs:
                (self.vault / reference).unlink(missing_ok=True)
            raise
        return self.public(version + 1)

    def effective(self, version=None):
        version, data = self.raw(version)
        cfg = self.settings.model_copy(deep=True)
        for role, item in data["roles"].items():
            setattr(cfg, KEY_FIELDS[role], SecretStr(self.secret(item.get("credential_ref"))))
            setattr(cfg, ENDPOINT_FIELDS[role], item["url"])
            setattr(cfg, MODEL_FIELDS[role], item["model"])
        cfg.recognizer_protocol = data["roles"]["recognizer"]["protocol"]
        cfg.supervisor_protocol = data["roles"]["supervisor"]["protocol"]
        cfg.role_timeouts = {r: d["timeout"] for r, d in data["roles"].items()}
        cfg.role_call_limits = {("title_generator" if r == "image" else r): d["limit"] for r, d in data["roles"].items()}
        cfg.max_calls_per_job = data["max_calls_per_job"]
        cfg.default_intro_asset_id = data["template"]["intro_asset_id"]
        cfg.title_style_fallback = data["template"]["fallback_style"]
        cfg.ending_policy = "freeze_for_closing_speech" if data["template"]["closing_hold"] else "review"
        return cfg
