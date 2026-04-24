# F.R.I.D.A.Y. - Tony Stark Demo

    "Fully Responsive Intelligent Digital Assistant for You"

This project has two parts that run together:

| Component | What it does |
|---|---|
| MCP Server (`uv run friday`) | FastMCP server that exposes tools over SSE (`/sse`) |
| Voice Agent (`uv run friday_voice`) | Voice MVP-based full-duplex pipeline using STT + LLM + TTS with MCP tool-calling |

## Architecture

```
Microphone -> STTHandler (RealtimeSTT + Whisper)
            -> FridayLLMHandler (Groq -> Ollama -> Gemini -> OpenAI fallback)
            -> MCP tools via FastMCP client (http://127.0.0.1:8000/sse)
            -> TTSHandler (Kokoro local by default, Cartesia optional)
            -> Speaker
```

## Project structure

```
friday-tony-stark-demo/
|- server.py                # MCP server entrypoint
|- agent_friday.py          # Voice runtime entrypoint (Voice MVP + MCP)
|- pyproject.toml
|- .env.example
`- friday/
   |- tools/                # MCP tools
   |- prompts/
   `- resources/
```

## Prerequisites

- Python 3.11+
- `uv`
- Local clone of `voice_engine_MVP`
  - Repository: `https://github.com/Makilesh/voice_engine_MVP`
  - Default lookup: `../voice_engine_MVP/src` relative to this repo
  - Or set `VOICE_ENGINE_MVP_SRC` in `.env`

## Setup

```bash
git clone https://github.com/Makilesh/F.R.I.D.A.Y.git
cd F.R.I.D.A.Y
uv sync
cp .env.example .env
```

Edit `.env` and set required keys (see below).

## Run

Start both terminals from the same host environment (for example, the same WSL distro or the same native shell).

Terminal 1 (MCP server):

```bash
uv run friday
```

Terminal 2 (voice agent):

```bash
uv run friday_voice
```

## Environment variables

### Configuration for voice pipeline

| Variable | Required | Notes |
|---|---|---|
| `OPENAI_API_KEY` | Conditional | Used if OpenAI is reached in fallback chain |
| `GEMINI_API_KEY` | Conditional | Used if Gemini is reached in fallback chain |
| `GROQ_API_KEY` | Conditional | Preferred first provider if set |
| `OLLAMA_ENABLED` | Conditional | `true`/`false`, enables local Ollama fallback |
| `OLLAMA_BASE_URL` | Optional | Default: `http://localhost:11434` |
| `OLLAMA_MODEL` | Optional | Default: `llama3.1:8b` |
| `USE_CARTESIA_TTS` | Optional | `false` uses local Kokoro (default), `true` uses Cartesia |
| `CARTESIA_API_KEY` | Optional | Needed only when `USE_CARTESIA_TTS=true` |
| `VOICE_ENGINE_MVP_SRC` | Optional | Absolute path to `voice_engine_MVP/src` |

### Required behavior

- At least one LLM provider must be configured:
  - `GROQ_API_KEY`, or
  - `GEMINI_API_KEY`, or
  - `OPENAI_API_KEY`, or
  - `OLLAMA_ENABLED=true`

## Scripts

| Command | Entry point |
|---|---|
| `uv run friday` | `server.py -> main()` |
| `uv run friday_voice` | `agent_friday.py -> main()` |

## Notes

- MCP server/tool code is unchanged; voice pipeline replacement is in `agent_friday.py`.
- FRIDAY system prompt/persona is preserved in `agent_friday.py`.
- Barge-in is supported through the Voice MVP STT/TTS handler integration.

## License

MIT
