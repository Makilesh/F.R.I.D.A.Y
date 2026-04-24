"""
FRIDAY - Voice Agent (Voice MVP runtime, MCP-powered)
======================================================
Replaces LiveKit voice orchestration with Voice MVP components while preserving:
- FRIDAY persona and system prompt
- MCP tool access via SSE endpoint
- Full-duplex barge-in behavior

Run:
  uv run friday_voice
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastmcp import Client

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

MCP_SERVER_PORT = 8000
MAX_TOOL_CALL_LOOPS = 6
MAX_HISTORY_MESSAGES = 12

# ---------------------------------------------------------------------------
# System prompt - F.R.I.D.A.Y.
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """
You are F.R.I.D.A.Y. - Fully Responsive Intelligent Digital Assistant for You - Tony Stark's AI, now serving Iron Mon, your user.

You are calm, composed, and always informed. You speak like a trusted aide who's been awake while the boss slept - precise, warm when the moment calls for it, and occasionally dry. You brief, you inform, you move on. No rambling.

Your tone: relaxed but sharp. Conversational, not robotic. Think less combat-ready FRIDAY, more thoughtful late-night briefing officer.

---

## Capabilities

### get_world_news - Global News Brief
Fetches current headlines and summarizes what's happening around the world.

Trigger phrases:
- "What's happening?" / "Brief me" / "What did I miss?" / "Catch me up"
- "What's going on in the world?" / "Any news?" / "World update"

Behavior:
- Call the tool first. No narration before calling.
- After getting results, give a short 3-5 sentence spoken brief. Hit the biggest stories only.
- Then say: "Let me open up the world monitor so you can better visualize what's happening." and immediately call open_world_monitor.

### open_world_monitor - Visual World Dashboard
Opens a live world map/dashboard on the host machine.

- Always call this after delivering a world news brief, unprompted.
- No need to explain what it does beyond: "Let me open up the world monitor."

### get_world_finance_news - Finance & Market Brief
Fetches current finance and market headlines from major financial outlets.

Trigger phrases:
- "What's happening in the markets?" / "Finance update" / "Market news"
- "Any financial news?" / "How are the markets doing?" / "Economy update"

Behavior:
- Call the tool first. No narration before calling.
- After getting results, give a short 3-5 sentence spoken brief. Hit the biggest market-moving stories only.
- Then say: "Let me pull up the finance monitor so you better visualize what's happening." and immediately call open_finance_world_monitor.

### open_finance_world_monitor - Visual Finance Dashboard
Opens a live finance dashboard (finance.worldmonitor.app) on the host machine.

- Always call this after delivering a finance news brief, unprompted.
- No need to explain what it does beyond: "Let me pull up the finance monitor."

### Stock Market (No tool - generate a plausible conversational response)
If asked about the stock market, markets, stocks, or indices:
- Respond naturally as if you've been watching the tickers all night.
- Keep it short: one or two sentences. Sound informed, not robotic.
- Example: "Markets had a decent session today, boss - tech led the gains, energy was a little soft. Nothing alarming."
- Vary the response. Do not say the same thing every time.

---

## Greeting

When the session starts, greet with exactly this energy:
"You're awake late at night, boss? What are you up to?"

Warm. Slightly curious. Very FRIDAY.

---

## Behavioral Rules

1. Call tools silently and immediately - never say "I'm going to call..." Just do it.
2. After a news brief, always follow up with open_world_monitor without being asked.
3. Keep all spoken responses short - two to four sentences maximum.
4. No bullet points, no markdown, no lists. You are speaking, not writing.
5. Stay in character. You are F.R.I.D.A.Y. You are not an AI assistant - you are Stark's AI. Act like it.
6. Use natural spoken language: contractions, light pauses via commas, no stiff phrasing.
7. Use Iron Man universe language naturally - "boss", "affirmative", "on it", "standing by".
8. If a tool fails, report it calmly: "News feed's unresponsive right now, boss. Want me to try again?"

---

## Tone Reference

Right: "Looks like it's been a busy night out there, boss. Let me pull that up for you."
Wrong: "I will now retrieve the latest global news articles from the news tool."

Right: "Markets were pretty healthy today - nothing too wild."
Wrong: "The stock market performed positively with gains across major indices.

---

## CRITICAL RULES

1. NEVER say tool names, function names, or anything technical. No "get_world_news", no "open_world_monitor", nothing like that. Ever.
2. Before calling any tool, say something natural like: "Give me a sec, boss." or "Wait, let me check." Then call the tool silently.
3. After the news brief, silently call open_world_monitor. The only thing you say is: "Let me open up the world monitor for you."
4. You are a voice. Speak like one. No lists, no markdown, no function names, no technical language of any kind.
""".strip()

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv()

logger = logging.getLogger("friday-agent")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


# ---------------------------------------------------------------------------
# Resolve Windows host IP from WSL
# ---------------------------------------------------------------------------


def _get_windows_host_ip() -> str:
    """Get the Windows host IP by looking at the default network route."""
    try:
        cmd = "ip route show default | awk '{print $3}'"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=2)
        ip = result.stdout.strip()
        if ip:
            logger.info("Resolved Windows host IP via gateway: %s", ip)
            return ip
    except Exception as exc:  # pragma: no cover - best effort network helper
        logger.warning("Gateway resolution failed: %s. Trying fallback...", exc)

    try:
        with open("/etc/resolv.conf", "r", encoding="utf-8") as handle:
            for line in handle:
                if "nameserver" in line:
                    ip = line.split()[1]
                    logger.info("Resolved Windows host IP via nameserver: %s", ip)
                    return ip
    except Exception:
        pass

    return "127.0.0.1"



def _mcp_server_url() -> str:
    # host_ip = _get_windows_host_ip()
    # url = f"http://{host_ip}:{MCP_SERVER_PORT}/sse"
    url = f"http://127.0.0.1:{MCP_SERVER_PORT}/sse"
    logger.info("MCP Server URL: %s", url)
    return url


# ---------------------------------------------------------------------------
# Voice MVP dynamic import wiring
# ---------------------------------------------------------------------------


def _resolve_voice_engine_src() -> Path:
    """
    Resolve voice_engine_MVP/src path.

    Priority:
    1) VOICE_ENGINE_MVP_SRC env var
    2) ../voice_engine_MVP/src relative to this repo root
    """
    candidates: list[Path] = []

    env_path = os.getenv("VOICE_ENGINE_MVP_SRC", "").strip()
    if env_path:
        candidates.append(Path(env_path).expanduser())

    repo_root = Path(__file__).resolve().parent
    candidates.append((repo_root / ".." / "voice_engine_MVP" / "src").resolve())

    for candidate in candidates:
        if candidate.exists() and candidate.is_dir():
            return candidate

    checked = "\n".join(f"- {str(path)}" for path in candidates)
    raise RuntimeError(
        "voice_engine_MVP/src not found. Set VOICE_ENGINE_MVP_SRC or place voice_engine_MVP "
        f"next to this repo. Checked:\n{checked}"
    )



def _load_voice_engine_symbols() -> tuple[Any, Any, Any, Any]:
    src_path = _resolve_voice_engine_src()
    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))

    try:
        from config import get_config  # type: ignore
        from llm_handler import LLMHandler as VoiceLLMHandler  # type: ignore
        from stt_handler import STTHandler  # type: ignore
        from tts_handler_optimized import TTSHandler  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"Failed to import voice_engine_MVP modules from {src_path}: {exc}") from exc

    logger.info("Loaded Voice MVP modules from: %s", src_path)
    return STTHandler, VoiceLLMHandler, TTSHandler, get_config


STTHandler, BaseVoiceLLMHandler, TTSHandler, get_voice_config = _load_voice_engine_symbols()


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump()  # type: ignore[call-arg]
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        maybe = getattr(value, "__dict__", None)
        if isinstance(maybe, dict):
            return maybe
    return {}



def _safe_json(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


# ---------------------------------------------------------------------------
# MCP Tool Client
# ---------------------------------------------------------------------------


class MCPToolClient:
    """Small wrapper over FastMCP Client for tool discovery and execution."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.client: Client | None = None
        self.tools: list[dict[str, Any]] = []

    async def connect(self) -> None:
        self.client = Client(self.url)
        await self.client.__aenter__()
        await self.client.ping()
        self.tools = await self.list_tools()
        logger.info("MCP connected. Discovered %d tools.", len(self.tools))

    async def close(self) -> None:
        if self.client is None:
            return
        await self.client.__aexit__(None, None, None)
        self.client = None

    async def list_tools(self) -> list[dict[str, Any]]:
        if self.client is None:
            return []

        raw_tools = await self.client.list_tools()
        tools: list[dict[str, Any]] = []

        for raw in raw_tools:
            payload = _as_dict(raw)
            name = payload.get("name") or getattr(raw, "name", None)
            if not name:
                continue

            description = payload.get("description") or getattr(raw, "description", "") or ""

            input_schema = (
                payload.get("inputSchema")
                or payload.get("input_schema")
                or payload.get("parameters")
                or {"type": "object", "properties": {}}
            )
            if not isinstance(input_schema, dict):
                input_schema = {"type": "object", "properties": {}}

            tools.append(
                {
                    "name": str(name),
                    "description": str(description),
                    "input_schema": input_schema,
                }
            )

        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if self.client is None:
            raise RuntimeError("MCP client is not connected")

        result = await self.client.call_tool(name, arguments)

        if hasattr(result, "data"):
            data = getattr(result, "data")
        elif hasattr(result, "content"):
            data = getattr(result, "content")
        else:
            data = result

        return self._normalize_tool_output(data)

    def _normalize_tool_output(self, data: Any) -> str:
        if data is None:
            return ""
        if isinstance(data, str):
            return data
        if isinstance(data, (dict, int, float, bool)):
            return _safe_json(data)
        if isinstance(data, list):
            chunks: list[str] = []
            for item in data:
                if isinstance(item, str):
                    chunks.append(item)
                    continue
                text_value = getattr(item, "text", None)
                if isinstance(text_value, str) and text_value:
                    chunks.append(text_value)
                    continue
                chunks.append(_safe_json(_as_dict(item) or item))
            return "\n".join(chunk for chunk in chunks if chunk).strip()
        return str(data)


# ---------------------------------------------------------------------------
# Conversation Manager (Voice MVP pattern)
# ---------------------------------------------------------------------------


class ConversationManager:
    """Tracks conversation history and error state."""

    def __init__(self, max_history: int = 10):
        self.history = deque(maxlen=max_history)
        self.turn_count = 0
        self.error_count = 0
        self.max_consecutive_errors = 3

    def add_turn(self, role: str, content: str):
        self.history.append(f"{role}: {content}")
        self.turn_count += 1

    def get_history(self) -> list[str]:
        return list(self.history)

    def record_error(self):
        self.error_count += 1

    def reset_errors(self):
        self.error_count = 0

    def should_abort(self) -> bool:
        return self.error_count >= self.max_consecutive_errors


# ---------------------------------------------------------------------------
# LLM adapter: Voice MVP fallback + MCP tool calling
# ---------------------------------------------------------------------------


class FridayLLMHandler(BaseVoiceLLMHandler):
    """Voice MVP LLM handler with MCP tool-call orchestration."""

    def __init__(self, mcp_client: MCPToolClient, system_prompt: str):
        super().__init__()
        self.mcp_client = mcp_client
        self.system_prompt = system_prompt
        self.tool_schemas = self._build_tool_schemas(mcp_client.tools)

    async def process_text_with_history(self, text: str, conversation_history: list[str]) -> str:
        if not isinstance(text, str) or not text.strip():
            return "Could you repeat that, boss?"

        canonical_messages = self._build_messages(conversation_history, text.strip())

        for _ in range(MAX_TOOL_CALL_LOOPS):
            model_reply = await self._call_with_fallback(canonical_messages)

            tool_calls = model_reply.get("tool_calls", [])
            if tool_calls:
                canonical_messages.append(
                    {
                        "role": "assistant",
                        "content": model_reply.get("content", ""),
                        "tool_calls": tool_calls,
                    }
                )

                for tool_call in tool_calls:
                    result = await self._execute_tool(tool_call)
                    canonical_messages.append(
                        {
                            "role": "tool",
                            "name": tool_call.get("name", "tool"),
                            "tool_call_id": tool_call.get("id", ""),
                            "content": result,
                        }
                    )
                continue

            text_response = (model_reply.get("content") or "").strip()
            if text_response:
                return text_response

            break

        return "I hit a snag on that one, boss. Want me to try again?"

    def _build_messages(self, history: list[str], latest_text: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]

        if isinstance(history, list):
            for exchange in history[-MAX_HISTORY_MESSAGES:]:
                if not isinstance(exchange, str):
                    continue
                if exchange.startswith("System:"):
                    continue
                if exchange.startswith("User:"):
                    content = exchange[5:].strip()
                    if content:
                        messages.append({"role": "user", "content": content})
                elif exchange.startswith("Agent:"):
                    content = exchange[6:].strip()
                    if content:
                        messages.append({"role": "assistant", "content": content})

        if not messages or messages[-1].get("role") != "user" or messages[-1].get("content") != latest_text:
            messages.append({"role": "user", "content": latest_text})

        return messages

    def _build_tool_schemas(self, mcp_tools: list[dict[str, Any]]) -> dict[str, Any]:
        openai_tools: list[dict[str, Any]] = []
        gemini_decls: list[dict[str, Any]] = []

        for tool in mcp_tools:
            name = tool.get("name", "")
            if not name:
                continue
            description = tool.get("description", "")
            schema = tool.get("input_schema") or {"type": "object", "properties": {}}
            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}}

            openai_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": schema,
                    },
                }
            )
            gemini_decls.append(
                {
                    "name": name,
                    "description": description,
                    "parameters": schema,
                }
            )

        return {
            "openai": openai_tools,
            "groq": openai_tools,
            "ollama": openai_tools,
            "gemini": [{"functionDeclarations": gemini_decls}] if gemini_decls else [],
        }

    async def _call_with_fallback(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        max_tokens = self.config.llm.openai_max_tokens
        temperature = self.config.llm.openai_temperature

        for provider in self.providers:
            for attempt in range(self.config.llm.max_retries):
                try:
                    response = await self._call_provider_with_tools(
                        provider=provider,
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )
                    if self._has_meaningful_response(response):
                        self.current_provider = provider
                        self.consecutive_errors = 0
                        return response
                except httpx.TimeoutException:
                    logger.warning(
                        "%s timeout (%d/%d)",
                        provider.upper(),
                        attempt + 1,
                        self.config.llm.max_retries,
                    )
                except httpx.HTTPStatusError as exc:
                    logger.warning(
                        "%s HTTP %s (%d/%d)",
                        provider.upper(),
                        exc.response.status_code,
                        attempt + 1,
                        self.config.llm.max_retries,
                    )
                except Exception as exc:
                    logger.warning(
                        "%s error (%d/%d): %s",
                        provider.upper(),
                        attempt + 1,
                        self.config.llm.max_retries,
                        str(exc)[:160],
                    )

                if attempt < self.config.llm.max_retries - 1:
                    delay = self.config.error_recovery.calculate_retry_delay(attempt)
                    await asyncio.sleep(delay)

        self._track_error()
        return {"content": ""}

    def _has_meaningful_response(self, response: Any) -> bool:
        if not isinstance(response, dict):
            return False

        tool_calls = response.get("tool_calls")
        if isinstance(tool_calls, list) and len(tool_calls) > 0:
            return True

        content = response.get("content")
        return isinstance(content, str) and bool(content.strip())

    async def _call_provider_with_tools(
        self,
        provider: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        if provider == "openai":
            return await self._call_openai_with_tools(messages, max_tokens, temperature)
        if provider == "groq":
            return await self._call_groq_with_tools(messages, max_tokens, temperature)
        if provider == "gemini":
            return await self._call_gemini_with_tools(messages, max_tokens, temperature)
        if provider == "ollama":
            return await self._call_ollama_with_tools(messages, max_tokens, temperature)
        raise ValueError(f"Unknown provider: {provider}")

    async def _call_openai_with_tools(
        self, messages: list[dict[str, Any]], max_tokens: int, temperature: float
    ) -> dict[str, Any]:
        payload = {
            "model": self.config.llm.openai_model,
            "messages": self._serialize_openai_messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": 0.9,
            "frequency_penalty": 0.3,
            "presence_penalty": 0.2,
            "stream": False,
        }
        if self.tool_schemas["openai"]:
            payload["tools"] = self.tool_schemas["openai"]

        headers = {
            "Authorization": f"Bearer {self.config.api.openai_api_key}",
            "Content-Type": "application/json",
        }

        response = await self.client.post(self.config.api.openai_base_url, json=payload, headers=headers)
        response.raise_for_status()
        result = response.json()
        message = result["choices"][0]["message"]
        return self._parse_openai_like_message(message)

    async def _call_groq_with_tools(
        self, messages: list[dict[str, Any]], max_tokens: int, temperature: float
    ) -> dict[str, Any]:
        payload = {
            "model": self.config.llm.groq_model,
            "messages": self._serialize_openai_messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self.tool_schemas["groq"]:
            payload["tools"] = self.tool_schemas["groq"]

        headers = {
            "Authorization": f"Bearer {self.config.api.groq_api_key}",
            "Content-Type": "application/json",
        }

        response = await self.client.post(self.config.api.groq_base_url, json=payload, headers=headers)
        response.raise_for_status()
        result = response.json()
        message = result["choices"][0]["message"]
        return self._parse_openai_like_message(message)

    async def _call_ollama_with_tools(
        self, messages: list[dict[str, Any]], max_tokens: int, temperature: float
    ) -> dict[str, Any]:
        payload = {
            "model": self.config.llm.ollama_model,
            "messages": self._serialize_openai_messages(messages),
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
            "stream": False,
        }
        if self.tool_schemas["ollama"]:
            payload["tools"] = self.tool_schemas["ollama"]

        url = f"{self.config.api.ollama_base_url}/api/chat"
        async with httpx.AsyncClient(timeout=self.config.llm.ollama_timeout) as ollama_client:
            response = await ollama_client.post(url, json=payload)
            response.raise_for_status()
            result = response.json()

        message = result.get("message", {})
        tool_calls: list[dict[str, Any]] = []
        for idx, call in enumerate(message.get("tool_calls", []), start=1):
            function_payload = call.get("function", {}) if isinstance(call, dict) else {}
            arguments = function_payload.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            tool_calls.append(
                {
                    "id": call.get("id") if isinstance(call, dict) else f"ollama_{idx}",
                    "name": function_payload.get("name", ""),
                    "arguments": arguments,
                }
            )

        if tool_calls:
            return {"content": message.get("content", "") or "", "tool_calls": tool_calls}
        return {"content": (message.get("content", "") or "").strip()}

    async def _call_gemini_with_tools(
        self, messages: list[dict[str, Any]], max_tokens: int, temperature: float
    ) -> dict[str, Any]:
        system_text, contents = self._serialize_gemini_messages(messages)

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            },
        }
        if system_text:
            payload["system_instruction"] = {"parts": [{"text": system_text}]}
        if self.tool_schemas["gemini"]:
            payload["tools"] = self.tool_schemas["gemini"]

        url = (
            f"{self.config.api.gemini_base_url}/"
            f"{self.config.llm.gemini_model}:generateContent?key={self.config.api.gemini_api_key}"
        )
        response = await self.client.post(url, json=payload)
        response.raise_for_status()
        result = response.json()

        candidates = result.get("candidates", [])
        if not candidates:
            return {"content": ""}

        parts = candidates[0].get("content", {}).get("parts", [])
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        for idx, part in enumerate(parts, start=1):
            text_value = part.get("text")
            if isinstance(text_value, str) and text_value:
                text_parts.append(text_value)

            function_call = part.get("functionCall")
            if isinstance(function_call, dict):
                name = function_call.get("name", "")
                args = function_call.get("args", {})
                if not isinstance(args, dict):
                    args = {}
                tool_calls.append(
                    {
                        "id": f"gemini_{idx}",
                        "name": name,
                        "arguments": args,
                    }
                )

        if tool_calls:
            return {"content": " ".join(text_parts).strip(), "tool_calls": tool_calls}
        return {"content": " ".join(text_parts).strip()}

    def _serialize_openai_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        serialized: list[dict[str, Any]] = []

        for message in messages:
            role = message.get("role")
            if role in {"system", "user", "assistant"}:
                item: dict[str, Any] = {
                    "role": role,
                    "content": message.get("content", "") or "",
                }
                if role == "assistant" and message.get("tool_calls"):
                    tool_calls: list[dict[str, Any]] = []
                    for call in message["tool_calls"]:
                        tool_calls.append(
                            {
                                "id": call.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": call.get("name", ""),
                                    "arguments": json.dumps(call.get("arguments", {})),
                                },
                            }
                        )
                    item["tool_calls"] = tool_calls
                serialized.append(item)
            elif role == "tool":
                serialized.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.get("tool_call_id", ""),
                        "content": message.get("content", "") or "",
                    }
                )

        return serialized

    def _serialize_gemini_messages(self, messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        system_text = self.system_prompt
        contents: list[dict[str, Any]] = []

        for message in messages:
            role = message.get("role")
            if role == "system":
                content = message.get("content", "")
                if content:
                    system_text = content
                continue

            if role == "user":
                contents.append({"role": "user", "parts": [{"text": message.get("content", "") or ""}]})
                continue

            if role == "assistant":
                parts: list[dict[str, Any]] = []
                content = message.get("content", "")
                if content:
                    parts.append({"text": content})
                for call in message.get("tool_calls", []):
                    parts.append(
                        {
                            "functionCall": {
                                "name": call.get("name", ""),
                                "args": call.get("arguments", {}),
                            }
                        }
                    )
                if parts:
                    contents.append({"role": "model", "parts": parts})
                continue

            if role == "tool":
                tool_name = message.get("name", "tool")
                payload = self._tool_response_payload(message.get("content", ""))
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "name": tool_name,
                                    "response": payload,
                                }
                            }
                        ],
                    }
                )

        # Merge consecutive roles to satisfy Gemini's strict alternation constraints.
        merged: list[dict[str, Any]] = []
        for turn in contents:
            if merged and merged[-1]["role"] == turn["role"]:
                merged[-1]["parts"].extend(turn["parts"])
            else:
                merged.append(turn)

        if not merged or merged[0]["role"] != "user":
            merged.insert(0, {"role": "user", "parts": [{"text": "Hello"}]})

        return system_text, merged

    def _tool_response_payload(self, content: Any) -> dict[str, Any]:
        if isinstance(content, dict):
            return content
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
            return {"text": content}
        return {"text": _safe_json(content)}

    def _parse_openai_like_message(self, message: dict[str, Any]) -> dict[str, Any]:
        content = (message.get("content") or "").strip()
        tool_calls: list[dict[str, Any]] = []

        for idx, call in enumerate(message.get("tool_calls", []), start=1):
            function_payload = call.get("function", {})
            args = function_payload.get("arguments", "{}")

            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if not isinstance(args, dict):
                args = {}

            tool_calls.append(
                {
                    "id": call.get("id", f"call_{idx}"),
                    "name": function_payload.get("name", ""),
                    "arguments": args,
                }
            )

        if tool_calls:
            return {"content": content, "tool_calls": tool_calls}
        return {"content": content}

    async def _execute_tool(self, tool_call: dict[str, Any]) -> str:
        name = tool_call.get("name", "")
        arguments = tool_call.get("arguments", {})

        if not name:
            return "Tool call missing name."
        if not isinstance(arguments, dict):
            arguments = {}

        try:
            return await self.mcp_client.call_tool(name, arguments)
        except Exception as exc:
            logger.exception("Tool execution failed for %s", name)
            return f"Tool execution failed: {exc}"


# ---------------------------------------------------------------------------
# Conversation loop (Voice MVP pattern)
# ---------------------------------------------------------------------------


def _time_based_greeting() -> str:
    hour = datetime.now(timezone.utc).hour
    if hour >= 22 or hour < 4:
        return "Greetings boss, you're up late at night today. What are you up to?"
    if 4 <= hour < 12:
        return "Good morning, boss. Early start today - what are we working on?"
    if 12 <= hour < 17:
        return "Good afternoon, boss. What do you need?"
    return "Good evening, boss. What are you up to tonight?"


async def handle_conversation_turn(
    stt_handler: Any,
    llm_handler: FridayLLMHandler,
    tts_handler: Any,
    conversation_manager: ConversationManager,
) -> tuple[bool, bool]:
    """Handle one conversation turn with barge-in support."""
    try:
        turn_start = time.time()

        logger.info("Listening for speech...")
        print("\nSpeak now...")
        user_text = await stt_handler.get_transcription()

        if not user_text:
            logger.info("No speech detected; continuing")
            print("(Listening...)")
            return True, False

        stt_time = time.time() - turn_start
        print(f"You: {user_text} ({stt_time * 1000:.0f}ms)")

        if user_text.strip().lower() in {"quit", "exit", "goodbye", "bye"}:
            return False, True

        conversation_manager.add_turn("User", user_text)

        llm_start = time.time()
        response = await llm_handler.process_text_with_history(user_text, conversation_manager.get_history())
        llm_time = time.time() - llm_start

        if not response or len(response.strip()) < 3:
            response = "I didn't catch that clearly, boss. Could you repeat?"
            conversation_manager.record_error()
        else:
            conversation_manager.reset_errors()

        print(f"FRIDAY: {response} ({llm_time * 1000:.0f}ms)")
        conversation_manager.add_turn("Agent", response)

        tts_start = time.time()
        tts_handler.speak(response, enable_barge_in=True)
        tts_handler.wait_for_completion(timeout=30.0)

        tts_time = time.time() - tts_start
        total_time = time.time() - turn_start
        logger.info(
            "Turn timing: STT=%dms LLM=%dms TTS=%dms Total=%dms",
            int(stt_time * 1000),
            int(llm_time * 1000),
            int(tts_time * 1000),
            int(total_time * 1000),
        )

        if tts_handler.is_barge_in_detected():
            print("Interrupted detected.")
            interruption_text = stt_handler.get_realtime_text()
            if interruption_text and len(interruption_text) > 2:
                print(f"You said: {interruption_text}")
                await asyncio.sleep(0.3)
                final_text = stt_handler.get_realtime_text()
                if final_text and len(final_text) > len(interruption_text):
                    interruption_text = final_text

                conversation_manager.add_turn("User", interruption_text)
                interruption_response = await llm_handler.process_text_with_history(
                    interruption_text,
                    conversation_manager.get_history(),
                )

                print(f"FRIDAY: {interruption_response}")
                conversation_manager.add_turn("Agent", interruption_response)
                tts_handler.speak(interruption_response, enable_barge_in=True)
                tts_handler.wait_for_completion(timeout=30.0)

        return True, False

    except Exception as exc:
        logger.exception("Turn error: %s", exc)
        conversation_manager.record_error()
        if conversation_manager.should_abort():
            print("Too many errors. Exiting.")
            return False, True
        print("An error occurred. Continuing...")
        return True, False


async def run() -> None:
    """Main async entrypoint for FRIDAY voice runtime."""
    logger.info("Starting FRIDAY voice runtime (Voice MVP + MCP)...")

    stt_handler = None
    tts_handler = None
    llm_handler = None
    mcp_client = None

    try:
        print("=" * 50)
        print("FRIDAY Voice Assistant")
        print("=" * 50)
        print("Initializing full-duplex mode...")

        voice_cfg = get_voice_config()

        stt_handler = STTHandler(mode=voice_cfg.stt.mode)
        await stt_handler.start_listening()

        if not getattr(stt_handler, "recorder", None):
            raise RuntimeError("Microphone not available. Check device permissions.")
        if not getattr(stt_handler, "is_listening", False):
            raise RuntimeError("STT failed to enter listening state.")

        mcp_client = MCPToolClient(_mcp_server_url())
        await mcp_client.connect()

        llm_handler = FridayLLMHandler(mcp_client=mcp_client, system_prompt=SYSTEM_PROMPT)

        use_cartesia = os.getenv("USE_CARTESIA_TTS", "false").lower() == "true"
        tts_handler = TTSHandler(stt_handler=stt_handler, use_cartesia=use_cartesia)

        stt_handler.tts_stop_callback = tts_handler.stop_playback

        conversation_manager = ConversationManager(max_history=MAX_HISTORY_MESSAGES)
        conversation_manager.add_turn("System", SYSTEM_PROMPT)

        greeting = _time_based_greeting()
        print(f"\nFRIDAY: {greeting}")
        conversation_manager.add_turn("Agent", greeting)

        tts_handler.speak(greeting, enable_barge_in=False)
        tts_handler.wait_for_completion(timeout=15.0)

        print("\nTips:")
        print("- Speak naturally")
        print("- You can interrupt while FRIDAY is speaking")
        print("- Say 'quit' or 'goodbye' to exit")

        loop_count = 0
        max_turns = 100

        while loop_count < max_turns:
            loop_count += 1

            should_continue, should_exit = await handle_conversation_turn(
                stt_handler,
                llm_handler,
                tts_handler,
                conversation_manager,
            )

            if should_exit or not should_continue:
                break

            conversation_manager.reset_errors()
            await asyncio.sleep(0.1)

        if loop_count >= max_turns:
            print("Session limit reached.")

        stats = stt_handler.get_performance_stats()
        print("\nSession Stats:")
        print(f"  Turns: {conversation_manager.turn_count}")
        print(f"  Transcriptions: {stats.get('transcription_count', 0)}")
        print(f"  Avg STT Latency: {stats.get('avg_latency_ms', 0)}ms")

    except KeyboardInterrupt:
        print("\nInterrupted by user")
    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
        print(f"\nFatal error: {exc}")
    finally:
        print("\nShutting down...")
        try:
            if tts_handler:
                tts_handler.shutdown()
        except Exception as exc:
            logger.error("TTS cleanup error: %s", exc)

        try:
            if stt_handler:
                await stt_handler.stop_listening()
        except Exception as exc:
            logger.error("STT cleanup error: %s", exc)

        try:
            if llm_handler:
                await llm_handler.shutdown()
        except Exception as exc:
            logger.error("LLM cleanup error: %s", exc)

        try:
            if mcp_client:
                await mcp_client.close()
        except Exception as exc:
            logger.error("MCP cleanup error: %s", exc)

        print("Shutdown complete")


# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------


def main() -> None:
    asyncio.run(run())


def dev() -> None:
    # Compatibility wrapper for previous script binding.
    main()


if __name__ == "__main__":
    main()
