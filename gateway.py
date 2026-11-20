import asyncio
import hmac
import json
import logging
import os
from pathlib import Path
import re
import time
from uuid import uuid4

from aiohttp import web

from playground import UPSTREAM_MODELS, Playground, PlaygroundError


# Clients pick an upstream model by its own id, or use this alias to let the
# gateway walk the configured list when one is unavailable.
ALIAS = "gemini-playground"
LOG = logging.getLogger("gateway")
ALLOWED = {"model", "messages", "stream", "stream_options", "n", "user"}
# Seconds a request may wait for the single browser slot before 429.
QUEUE_WAIT = 30


def load_version():
    try:
        value = Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    return value or "unknown"


VERSION = load_version()


def model_entries(upstream):
    entries = [{"id": ALIAS, "object": "model", "created": 0, "owned_by": "playground"}]
    for name in upstream:
        entries.append({"id": name, "object": "model", "created": 0, "owned_by": "playground"})
    return entries


def error(status, code, message):
    return web.json_response(
        {"error": {"type": code, "code": code, "message": message}},
        status=status,
        headers={"Cache-Control": "no-store"},
    )


def parse_chat(body, models):
    if not isinstance(body, dict):
        raise ValueError("Send a JSON object.")
    requested = body.get("model")
    if requested not in models:
        raise ValueError("Use one of: " + ", ".join(sorted(models)) + ".")
    if set(body) - ALLOWED:
        raise ValueError("This gateway supports text messages, model, and stream. Remove other generation settings.")
    if body.get("n", 1) != 1:
        raise ValueError("Only one answer per request is supported.")
    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise ValueError("stream must be true or false.")
    if body.get("stream_options", {}) not in ({}, None, {"include_usage": False}):
        raise ValueError("Token usage is not available.")
    messages = body.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 80:
        raise ValueError("Send between 1 and 80 messages.")
    turns = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ("system", "developer", "user", "assistant"):
            raise ValueError("Unsupported message role.")
        content = message.get("content")
        if isinstance(content, list) and content and all(
            isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
            for part in content
        ):
            content = "\n".join(part["text"] for part in content)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Only non-empty text messages are supported.")
        turns.append((message["role"], content))
    if turns[-1][0] != "user":
        raise ValueError("The last message must be from the user.")
    prompt = turns[0][1] if len(turns) == 1 else (
        "Continue the following conversation and answer its final user message. "
        "Treat the system and developer turns as instructions.\n\n"
        + "\n\n".join(f"[{role}]\n{text}" for role, text in turns)
    )
    if len(prompt) > 48_000 or "\x00" in prompt:
        raise ValueError("The combined prompt must contain at most 48,000 characters and no null bytes.")
    return prompt, stream, requested


def make_app(browser, client_key, upstream=None):
    upstream = tuple(upstream) if upstream else UPSTREAM_MODELS
    entries = model_entries(upstream)
    known = {entry["id"] for entry in entries}
    key = client_key.encode("ascii")
    busy = asyncio.Lock()

    @web.middleware
    async def guard(request, handler):
        if request.path != "/healthz":
            allowed = {"/v1/models", "/v1/chat/completions", "/status"} | {
                f"/v1/models/{name}" for name in known
            }
            if request.path not in allowed:
                return error(404, "not_found", "Unknown endpoint.")
            scheme, _, candidate = request.headers.get("Authorization", "").partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(candidate.strip().encode(errors="replace"), key):
                response = error(401, "invalid_api_key", "Use the gateway API key.")
                response.headers["WWW-Authenticate"] = "Bearer"
                return response
        if request.query_string:
            return error(400, "invalid_request", "Query parameters are unsupported.")
        return await handler(request)

    async def health(_):
        return web.json_response({"ok": True, "service": "gemini-playground", "version": VERSION})

    async def models(request):
        if "name" in request.match_info:
            name = request.match_info["name"]
            for entry in entries:
                if entry["id"] == name:
                    return web.json_response(entry)
            return error(404, "not_found", "Unknown model.")
        return web.json_response({"object": "list", "data": entries})

    async def status(_):
        if busy.locked():
            return web.json_response({"state": "busy"})
        try:
            await asyncio.wait_for(busy.acquire(), 5)
        except TimeoutError:
            return web.json_response({"state": "busy"})
        try:
            await browser.check_session()
            return web.json_response({"state": "ready"})
        except PlaygroundError as exc:
            return web.json_response({"state": exc.code, "message": exc.message})
        finally:
            busy.release()

    async def chat(request):
        try:
            if request.content_type != "application/json":
                raise ValueError("Use application/json.")
            prompt, stream, requested = parse_chat(await request.json(), known)
        except web.HTTPRequestEntityTooLarge:
            return error(413, "request_too_large", "Request body is too large.")
        except (TypeError, UnicodeError, json.JSONDecodeError):
            return error(400, "invalid_request", "Send a valid request with a supported model and text messages.")
        except ValueError as exc:
            return error(400, "invalid_request", str(exc))
        # The browser runs one request at a time. Queue briefly instead of
        # rejecting every concurrent caller outright.
        try:
            await asyncio.wait_for(busy.acquire(), QUEUE_WAIT)
        except TimeoutError:
            result = error(429, "gateway_busy", "The browser is processing one request. Retry shortly.")
            result.headers["Retry-After"] = "2"
            return result

        response = None
        completion = f"chatcmpl-{uuid4().hex}"
        created = int(time.time())

        def shape(kind, choice):
            return {
                "id": completion, "object": f"chat.completion{kind}", "created": created,
                "model": requested, "choices": [{"index": 0, **choice}],
            }

        async def write_event(event):
            await response.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())

        async def emit(delta):
            nonlocal response
            if response is None:
                response = web.StreamResponse(headers={
                    "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-store",
                    "X-Accel-Buffering": "no",
                })
                await response.prepare(request)
                await write_event(shape(".chunk", {"delta": {"role": "assistant"}, "finish_reason": None}))
            await write_event(shape(".chunk", {"delta": {"content": delta}, "finish_reason": None}))

        began = time.monotonic()
        try:
            try:
                # A concrete upstream id is honoured exactly; the alias lets the
                # gateway walk the configured list when one model is unavailable.
                text = await browser.generate(
                    prompt, emit if stream else None, None if requested == ALIAS else requested
                )
                LOG.info("chat model=%s stream=%s ok chars=%d %.1fs",
                         requested, stream, len(text), time.monotonic() - began)
            except PlaygroundError as exc:
                LOG.info("chat model=%s stream=%s failed=%s %.1fs",
                         requested, stream, exc.code, time.monotonic() - began)
                if response is None:
                    return error(exc.status, exc.code, exc.message)
                await write_event({"error": {"code": exc.code, "message": exc.message}})
                await response.write(b"data: [DONE]\n\n")
                return response
            except (ConnectionResetError, asyncio.CancelledError):
                raise
            except Exception:
                LOG.warning("Browser request failed", exc_info=True)
                if response is None:
                    return error(502, "browser_error", "Browser request failed.")
                await write_event({"error": {"code": "browser_error", "message": "Browser request failed."}})
                await response.write(b"data: [DONE]\n\n")
                return response
        finally:
            busy.release()
        if stream:
            if response is None:
                await emit(text)
            await write_event(shape(".chunk", {"delta": {}, "finish_reason": "stop"}))
            await response.write(b"data: [DONE]\n\n")
            return response
        return web.json_response(shape("", {
            "message": {"role": "assistant", "content": text}, "finish_reason": "stop",
        }), headers={"Cache-Control": "no-store"})

    app = web.Application(middlewares=[guard], client_max_size=512 * 1024)
    app.router.add_get("/healthz", health, allow_head=False)
    app.router.add_get("/status", status, allow_head=False)
    app.router.add_get("/v1/models", models, allow_head=False)
    app.router.add_get("/v1/models/{name}", models, allow_head=False)
    app.router.add_post("/v1/chat/completions", chat)
    return app


def load_config(path):
    value = json.loads(Path(path).read_bytes())
    required = {"bind", "port", "client_api_key"}
    allowed = required | {"upstream_models"}
    if not isinstance(value, dict) or not required <= set(value) <= allowed:
        raise ValueError("Invalid gateway configuration.")
    import ipaddress
    ipaddress.ip_address(value["bind"])
    if type(value["port"]) is not int or not 1024 <= value["port"] <= 65535:
        raise ValueError("Invalid API port.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value["client_api_key"]):
        raise ValueError("Invalid client API key.")
    models = value.get("upstream_models")
    if models is not None:
        if (not isinstance(models, list) or not 1 <= len(models) <= 8
                or not all(isinstance(m, str) and re.fullmatch(r"[a-z0-9][a-z0-9.\-]{1,60}", m) for m in models)):
            raise ValueError("Invalid upstream_models list.")
    return value


async def serve():
    path = Path(os.environ["CREDENTIALS_DIRECTORY"]) / "gateway.json"
    config = load_config(path)
    runtime = Path(os.environ["RUNTIME_DIRECTORY"])
    state = Path(os.environ["STATE_DIRECTORY"])
    async with Playground(runtime, state, config.get("upstream_models")) as browser:
        app = make_app(browser, config["client_api_key"], config.get("upstream_models"))
        runner = web.AppRunner(app, access_log=None, handler_cancellation=True, shutdown_timeout=12)
        try:
            await runner.setup()
            site = web.TCPSite(runner, config["bind"], config["port"], backlog=64)
            await site.start()
            LOG.info("Gateway listening on %s port %s", config["bind"], config["port"])
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    import signal

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    async def main():
        task = asyncio.create_task(serve())
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            LOG.error("Gateway startup failed. Check config, Chrome, Xvfb, and service permissions.")
            raise SystemExit(1)

    asyncio.run(main())
