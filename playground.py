import asyncio
import json
from pathlib import Path
import re
import socket
from urllib.parse import urlsplit
from urllib.request import urlopen

from playwright.async_api import Error as BrowserError, async_playwright

from display import spawn, stop_process


STUDIO = "https://aistudio.google.com"
# AI Studio's BotGuard attestation rejects automated browsers with 403
# "The caller does not have permission". Use a normal desktop Chrome UA and
# no automation markers, as a plain headless Chrome would.
NORMAL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36"
)
# Upstream AI Studio models tried in order. The page's own default model may
# not be unlocked for the signed-in account (403) or may be overloaded (503),
# so the gateway pins a known-good model and falls back on transient errors.
UPSTREAM_MODELS = ("gemini-4-hydrogen", "gemini-4-neon", "gemini-4-argon")
CHAT = STUDIO + "/prompts/new_chat?temporary=true&model=" + UPSTREAM_MODELS[0]
# Google refuses a first send now and then while the next send from the same
# page succeeds, so a refused attempt is retried on the warm page.
RETRIES = 6
# 429 means the model's usage limit is exhausted: retrying cannot help and
# burns quota, so it is surfaced instead (the caller may pick another model).
# 504 is a stuck page: retrying every 150s timeout would hold the gateway for
# many minutes, so it is surfaced too.
RETRYABLE = (403, 503)
# If the page has not fired GenerateContent within this many seconds, it is
# stuck: fail the attempt so it can retry instead of blocking for 150s.
NO_SEND_TIMEOUT = 20


def chat_url(model):
    return STUDIO + "/prompts/new_chat?temporary=true&model=" + model


# Injected at document-start: tee the page's own GenerateContent response so the
# gateway reads the raw protobuf-as-JSON stream (clean text + true streaming)
# instead of scraping rendered DOM. AI Studio sends GenerateContent over XHR
# (Angular/grpc-web); the page resets the XHR to readyState 0 after reading, so
# capture incremental responseText on progress/readystatechange, not load.
STREAM_TEE_JS = r"""
(() => {
  if (window.__pgbTee) return;
  window.__pgbTee = true;
  const g = window.__pgb = { seq: 0, events: [] };
  const isGen = (u) => typeof u === 'string' && u.indexOf('MakerSuiteService/GenerateContent') >= 0;
  const XO = XMLHttpRequest.prototype.open, XS = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, url) { this.__pgbUrl = url; return XO.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function (body) {
    const url = this.__pgbUrl || '';
    if (isGen(url)) {
      const id = ++g.seq; const xhr = this; let last = 0; let ended = false;
      const finish = () => { if (!ended) { ended = true; g.events.push({ type: 'end', id }); } };
      const grab = () => {
        if (xhr.readyState === 2) g.events.push({ type: 'meta', id, status: xhr.status });
        if (xhr.readyState >= 3) {
          let t = ''; try { t = xhr.responseText || ''; } catch (e) {}
          if (t.length > last) { g.events.push({ type: 'chunk', id, text: t.slice(last) }); last = t.length; }
          if (xhr.readyState === 4) finish();
        }
        if (xhr.readyState === 0 && last > 0) finish();
      };
      xhr.addEventListener('readystatechange', grab);
      xhr.addEventListener('progress', grab);
      xhr.addEventListener('error', () => { g.events.push({ type: 'meta', id, status: xhr.status || 0 }); finish(); });
      xhr.addEventListener('abort', finish);
      xhr.addEventListener('loadend', finish);
    }
    return XS.apply(this, arguments);
  };
  const of = window.fetch;
  window.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    if (!isGen(url)) return of.apply(this, arguments);
    const id = ++g.seq;
    return of.apply(this, arguments).then((resp) => {
      g.events.push({ type: 'meta', id, status: resp.status });
      if (!resp.body) { g.events.push({ type: 'end', id }); return resp; }
      const br = resp.body.tee(); const rd = br[0].getReader(); const dec = new TextDecoder();
      const step = () => rd.read().then((r) => {
        if (r.done) { const tail = dec.decode(); if (tail) g.events.push({ type: 'chunk', id, text: tail }); g.events.push({ type: 'end', id }); return; }
        const text = dec.decode(r.value, { stream: true }); if (text) g.events.push({ type: 'chunk', id, text }); return step();
      }).catch(() => g.events.push({ type: 'end', id }));
      step();
      return new Response(br[1], { status: resp.status, statusText: resp.statusText, headers: resp.headers });
    }, (err) => { g.events.push({ type: 'meta', id, status: 0 }); g.events.push({ type: 'end', id }); throw err; });
  };
})();
"""
DRAIN_JS = "() => { const g = window.__pgb; if (!g) return []; const e = g.events; g.events = []; return e; }"


def upstream_error(status, body):
    code, message = None, ""
    text = body.lstrip(" \r\n\t")
    if text.startswith(")]}'"):
        text = text[4:].lstrip(" \r\n\t")
    try:
        payload = json.loads(text)
        if isinstance(payload, list) and len(payload) > 1 and isinstance(payload[1], list) and payload[1]:
            code = payload[1][0]
            if len(payload[1]) > 1 and isinstance(payload[1][1], str):
                message = payload[1][1]
    except (ValueError, TypeError, IndexError):
        # Google's error bodies are not strict JSON (leading empty element),
        # e.g. [,[7,"The caller does not have permission"]].
        found = re.search(r'\[\s*(\d+)\s*,\s*"([^"]*)"', text)
        if found:
            code, message = int(found.group(1)), found.group(2)
    if status == 403 or code == 7:
        return PlaygroundError(403, "google_permission_denied", message or "AI Studio refused to generate for this session.")
    if status == 429 or code == 8:
        return PlaygroundError(429, "google_quota", message or "AI Studio reported a quota or request limit.")
    if status == 503 or code == 14:
        return PlaygroundError(503, "google_unavailable", message or "The model is temporarily unavailable. Retry shortly.")
    return PlaygroundError(status or 502, "upstream_http_error", message or f"Upstream HTTP {status}.")


class WireFrame:
    __slots__ = ("texts", "finish", "usage")

    def __init__(self):
        self.texts, self.finish, self.usage = [], None, None


def _dig(value, *path):
    for index in path:
        if not isinstance(value, list) or index >= len(value):
            return None
        value = value[index]
    return value


def _is_content_node(value):
    return (isinstance(value, list) and len(value) >= 2
            and isinstance(value[0], list) and value[1] in ("model", "user"))


def _find_content_node(value):
    if not isinstance(value, list):
        return None
    if _is_content_node(value):
        return value
    for child in value:
        found = _find_content_node(child)
        if found is not None:
            return found
    return None


def deep_texts(value, out):
    """Collect every model text part in a parsed payload, tolerating any nesting."""
    if isinstance(value, list):
        if _is_content_node(value) and isinstance(value[0], list):
            for part in value[0]:
                if not isinstance(part, list) or len(part) < 2:
                    continue
                if len(part) > 12 and part[12] in (True, 1):
                    continue
                if isinstance(part[1], str) and part[1].strip():
                    out.append(part[1])
            return
        for child in value:
            deep_texts(child, out)


def parse_frame(frame):
    """Port of pg-bridge parseFrame: extract text deltas, finish and usage."""
    out = WireFrame()
    content = _dig(frame, 0, 0, 0)
    if not _is_content_node(content):
        content = _find_content_node(frame)
    if _is_content_node(content) and isinstance(content[0], list):
        for part in content[0]:
            if not isinstance(part, list) or len(part) < 2:
                continue
            if len(part) > 12 and part[12] in (True, 1):
                continue  # thought summary
            if isinstance(part[1], str):
                out.texts.append(part[1])
    finish = _dig(frame, 0, 0, 1)
    if isinstance(finish, (int, float)):
        out.finish = int(finish)
    usage = _dig(frame, 2)
    if isinstance(usage, list) and len(usage) >= 2:
        out.usage = [usage[0] if isinstance(usage[0], (int, float)) else 0,
                     usage[1] if isinstance(usage[1], (int, float)) else 0]
    return out


def _scan_value(data, start):
    length = len(data)
    if start >= length:
        return "", 0, False
    char = data[start]
    if char in "[{":
        depth, in_string, escaped = 0, False, False
        for i in range(start, length):
            c = data[i]
            if in_string:
                if escaped:
                    escaped = False
                elif c == "\\":
                    escaped = True
                elif c == '"':
                    in_string = False
                continue
            if c == '"':
                in_string = True
            elif c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    return (data[start:end], end + 1, True) if end < length else ("", 0, False)
        return "", 0, False
    if char == '"':
        in_string, escaped = False, False
        for i in range(start, length):
            c = data[i]
            if in_string:
                if escaped:
                    escaped = False
                elif c == "\\":
                    escaped = True
                elif c == '"':
                    end = i + 1
                    return (data[start:end], end + 1, True) if end < length else ("", 0, False)
            elif c == '"':
                in_string = True
        return "", 0, False
    for j in range(start, length):
        if data[j] == ",":
            return data[start:j], j + 1, True
        if data[j] == "]":
            return data[start:j], j, True
    return "", 0, False


class WireStream:
    """Port of pg-bridge streamAssembler: incremental top-level frame scanner."""

    def __init__(self):
        self.buf, self.consumed = "", 0
        self.trimmed = False
        # Number of leading '[' already stripped (the body wraps frames twice).
        self.outer_skipped = 0

    def feed(self, delta):
        self.buf += delta
        return self._drain(False)

    def finish(self):
        return self._drain(True)

    def _drain(self, final):
        data = self.buf
        if not self.trimmed:
            trimmed = data.lstrip(" \r\n\t")
            if trimmed.startswith(")]}'"):
                data = trimmed[4:].lstrip(" \r\n\t")
                self.buf, self.consumed = data, 0
            elif trimmed:
                self.trimmed = True
        out = []
        while True:
            element, nxt, ok = self._next_element(data)
            if not ok or nxt <= self.consumed:
                # No progress would loop forever on the closing bracket of the
                # outer array; the final pass below handles any remainder.
                break
            self.consumed = nxt
            try:
                out.append(parse_frame(json.loads(element)))
            except (ValueError, TypeError):
                pass
        if final:
            rest = data[min(self.consumed, len(data)):].strip()
            if rest.startswith(")]}'"):
                rest = rest[4:].strip()
            if rest:
                try:
                    out.append(parse_frame(json.loads(rest)))
                except (ValueError, TypeError):
                    pass
        return out

    def _next_element(self, data):
        i, length = self.consumed, len(data)
        while i < length and data[i] in " \n\r\t,":
            i += 1
        if i >= length:
            return "", 0, False
        if data[i] == "]":
            return "", 0, False  # end of the outer array
        if self.outer_skipped < 2 and data[i] == "[":
            # The body wraps the frames in two arrays: [[frame, frame, ...]].
            # Strip both opening brackets, resumably: the split may fall
            # between them, and a half-done skip used to truncate the answer.
            while self.outer_skipped < 2 and i < length and data[i] == "[":
                i += 1
                self.outer_skipped += 1
                while i < length and data[i] in " \n\r\t":
                    i += 1
            self.consumed = i
            if self.outer_skipped < 2 or i >= length:
                return "", 0, False
        return _scan_value(data, i)
COMPOSERS = (
    "ms-prompt-box textarea", "ms-prompt-box [contenteditable=true]",
    "textarea[aria-label*='prompt' i]", "textarea[placeholder*='prompt' i]",
    "textarea[placeholder*='message' i]",
)
ANSWERS = (
    "[data-turn-role=model] [data-response-text]",
    "ms-chat-turn .model-prompt-container .turn-content",
    "ms-chat-turn .model-prompt-container",
    "ms-chat-turn .model-response-container",
    "ms-chat-turn[role=article][aria-label*='model response' i]",
    "[data-message-author-role=model]",
)


class PlaygroundError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


async def visible_first(page, selectors):
    for selector in selectors:
        matches = page.locator(selector)
        for index in range(min(await matches.count(), 4)):
            item = matches.nth(index)
            if await item.is_visible():
                return item
    return None


class StudioUI:
    """Operates the visible chat controls and reads rendered response text."""

    def __init__(self, origin=STUDIO, chat=CHAT):
        self.origin, self.chat = origin, chat

    async def check(self, page):
        url = urlsplit(page.url)
        if url.hostname == "accounts.google.com":
            if "/challenge/" in url.path or "challenge" in url.path:
                raise PlaygroundError(403, "google_verification", "Complete Google's verification yourself in the login window.")
            raise PlaygroundError(503, "login_required", "Sign in to Google in the login window.")
        if f"{url.scheme}://{url.netloc}" != self.origin:
            raise PlaygroundError(403, "unexpected_page", "AI Studio opened another page. Review it in the login window.")
        if await page.locator("iframe[src*='recaptcha'][src*='/bframe']:visible, iframe[title*='recaptcha challenge' i]:visible").count():
            raise PlaygroundError(403, "google_verification", "Google is asking for verification. Use the login window.")
        alerts = page.locator("[role=alert]:visible, [role=alertdialog]:visible, mat-snack-bar-container:visible")
        for text in await alerts.all_text_contents():
            message = " ".join(text.split())
            low = message.lower()
            if re.search(r"usage limit|usage cap|quota|rate limit|too many requests|resource exhausted", low):
                raise PlaygroundError(429, "google_quota", message[:300] or "AI Studio reported a quota or request limit.")
            if "permission denied" in low:
                raise PlaygroundError(403, "google_permission_denied", message[:300] or "AI Studio refused to generate for this session.")
            if re.search(r"verify|captcha|unusual traffic|access denied|not available|permission", low):
                raise PlaygroundError(403, "google_restriction", message[:300] or "AI Studio reported a verification or access restriction.")
        sign_in = re.compile(r"^(Sign in( with Google)?|登录)$", re.I)
        if await page.get_by_role("button", name=sign_in).first.is_visible() or await page.get_by_role("link", name=sign_in).first.is_visible():
            raise PlaygroundError(503, "login_required", "Sign in to Google in the login window.")

    async def composer(self, page):
        deadline = asyncio.get_running_loop().time() + 18
        while asyncio.get_running_loop().time() < deadline:
            await self.check(page)
            composer = await visible_first(page, COMPOSERS)
            if composer is not None:
                return composer
            await asyncio.sleep(0.25)
        raise PlaygroundError(503, "chat_unavailable", "The AI Studio chat box was not found. Review the login window.")

    async def fill(self, page, prompt):
        composer = await self.composer(page)
        notice = page.locator(".glue-cookie-notification-bar:visible").first
        if await notice.is_visible():
            dismiss = notice.get_by_role(
                "button", name=re.compile(r"^(?:No,?\s+thanks|(?:OK,?\s+)?Got it|Dismiss)[.!]?$", re.I),
            ).first
            if not await dismiss.is_visible():
                raise PlaygroundError(409, "cookie_notice", "Review the cookie notice in the login window.")
            await dismiss.click(timeout=4000)
        prompt = prompt.replace("\r\n", "\n").replace("\r", "\n")
        await composer.fill(prompt, timeout=5000)
        entered = await composer.evaluate("(editor) => editor.value ?? editor.innerText")
        if entered != prompt:
            raise PlaygroundError(502, "prompt_not_entered", "The editor did not keep the complete prompt. Nothing was submitted.")
        run = page.get_by_role("button", name=re.compile(r"^((Run|Send)( prompt| message)?|运行|发送)(?:\s+.*)?$", re.I))
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            await self.check(page)
            for index in range(min(await run.count(), 6)):
                button = run.nth(index)
                if await button.is_visible() and await button.is_enabled():
                    await button.click(timeout=4000)
                    return
            await asyncio.sleep(.15)
        # Pass Google's own wording through instead of a generic message.
        detail = ""
        try:
            for text in await page.locator(
                "mat-snack-bar-container:visible, [role=alert]:visible, [role=alertdialog]:visible"
            ).all_text_contents():
                text = " ".join(text.split())
                if text:
                    detail = text[:300]
                    break
        except BrowserError:
            pass
        raise PlaygroundError(
            503, "send_unavailable",
            detail or "The AI Studio Run button is unavailable. Check the login window.",
        )

    async def read(self, page):
        for selector in ANSWERS:
            matches = page.locator(selector)
            if not await matches.count():
                continue
            answer = matches.last
            if not await answer.is_visible():
                continue
            result = await answer.inner_text(timeout=1200)
            result = result.strip()
            if result:
                return result
        return ""

    async def open_chat(self, page):
        navigation = await page.goto(self.chat, wait_until="domcontentloaded", timeout=35_000)
        if navigation and navigation.status in (403, 429):
            raise PlaygroundError(navigation.status, "google_restriction", "AI Studio denied or limited this request.")

    async def dismiss_notices(self, page):
        """Close the snack bar a refused generation leaves behind."""
        try:
            bars = page.locator(
                "mat-snack-bar-container:visible, .mat-mdc-snack-bar-container:visible"
            )
            for index in range(min(await bars.count(), 4)):
                button = bars.nth(index).get_by_role("button").first
                if await button.is_visible():
                    await button.click(timeout=1500)
            await asyncio.sleep(0.4)
        except BrowserError:
            pass

    async def generate(self, page, prompt, emit=None):
        """Generate, retrying a refused attempt on the same, already-warm page.

        Google refuses a first send now and then (403 "The caller does not have
        permission") while the next send from the same page succeeds, so a
        refused attempt is retried instead of surfaced. Retries stop as soon as
        text has been relayed, because a stream cannot be rewound.
        """
        await self.open_chat(page)
        relayed = False

        async def relay(delta):
            nonlocal relayed
            relayed = True
            if emit:
                await emit(delta)

        failure = None
        for attempt in range(RETRIES):
            if attempt:
                await self.dismiss_notices(page)
                await asyncio.sleep(min(0.6 + 0.4 * attempt, 2.5))
            try:
                return await self.attempt(page, prompt, relay)
            except PlaygroundError as exc:
                if relayed or exc.status not in RETRYABLE:
                    raise
                failure = exc
        raise failure

    async def attempt(self, page, prompt, relay):
        await page.evaluate(DRAIN_JS)
        await self.fill(page, prompt)
        stream = WireStream()
        pieces, raw = [], []
        status, ended = None, False
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        deadline = started_at + 150

        while loop.time() < deadline:
            for event in await page.evaluate(DRAIN_JS):
                kind = event.get("type")
                if kind == "meta":
                    status = event.get("status")
                elif kind == "chunk":
                    text = event.get("text", "")
                    raw.append(text)
                    if status is not None and status >= 400:
                        continue
                    for frame in stream.feed(text):
                        for chunk in frame.texts:
                            if not chunk:
                                continue
                            pieces.append(chunk)
                            await relay(chunk)
                elif kind == "end":
                    ended = True
            if ended:
                for frame in stream.finish():
                    for chunk in frame.texts:
                        if not chunk:
                            continue
                        pieces.append(chunk)
                        await relay(chunk)
                break
            if status is None and loop.time() - started_at > NO_SEND_TIMEOUT:
                # The page never fired GenerateContent: it is stuck, and waiting
                # out the full generation deadline would just block the gateway.
                raise PlaygroundError(
                    503, "send_timeout",
                    "AI Studio did not start generating for this prompt. Retry shortly.",
                )
            if urlsplit(page.url).hostname == "accounts.google.com":
                raise PlaygroundError(503, "login_required", "Sign in to Google in the login window.")
            await asyncio.sleep(0.12)
        if status is not None and status >= 400:
            raise upstream_error(status, "".join(raw))
        if not ended:
            raise PlaygroundError(504, "generation_timeout", "The AI Studio page did not finish generating in time.")
        text = "".join(pieces)
        if not text.strip():
            # Safety net: re-parse the whole payload and walk every frame,
            # in case the incremental scanner stopped at a nesting variant.
            whole = "".join(raw).lstrip(" \r\n\t")
            if whole.startswith(")]}'"):
                whole = whole[4:].lstrip(" \r\n\t")
            try:
                recovered = []
                deep_texts(json.loads(whole), recovered)
            except (ValueError, TypeError):
                recovered = []
            if recovered:
                for chunk in recovered:
                    await relay(chunk)
                return "".join(recovered)
            # Some refusals arrive as HTTP 200 carrying only an error frame.
            if whole.strip():
                raise upstream_error(status or 502, whole)
            raise PlaygroundError(502, "empty_response", "AI Studio returned no text for this request.")
        return text


def free_loopback_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Playground:
    """Drives a native Chrome through Playwright-over-CDP.

    Chrome is launched by us rather than by Playwright: a normal desktop UA,
    no automation markers and `--headless=new`. Playwright's own launcher adds
    flags that stall AI Studio's BotGuard send pipeline, so we only attach to
    the debug endpoint instead of letting Playwright launch.
    """

    def __init__(self, runtime, state, models=None):
        self.runtime = Path(runtime)
        self.state = Path(state)
        self.models = tuple(models) if models else UPSTREAM_MODELS
        self.ui = StudioUI()
        self.driver = self.browser = self.context = self.chrome = None
        self.closed = False

    def mark_closed(self, _):
        self.closed = True

    def chrome_flags(self, port):
        return [
            f"--user-data-dir={self.state / 'chrome-profile'}",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--no-sandbox",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-agent={NORMAL_UA}",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-features=Translate,BackForwardCache",
            "--autoplay-policy=no-user-gesture-required",
            # Must match desktop.py's login launch: the profile's cookies are
            # sealed with Chrome's basic password store.
            "--password-store=basic",
            "--window-size=1280,900",
            "--lang=en-US",
            "--headless=new",
        ]

    async def wait_debug_endpoint(self, port, timeout=30):
        deadline = asyncio.get_running_loop().time() + timeout
        url = f"http://127.0.0.1:{port}/json/version"
        while asyncio.get_running_loop().time() < deadline:
            if self.chrome.returncode is not None:
                raise PlaygroundError(503, "browser_unavailable", "Chrome exited during startup.")
            try:
                body = await asyncio.to_thread(lambda: urlopen(url, timeout=2).read())
                return json.loads(body)["webSocketDebuggerUrl"]
            except Exception:
                await asyncio.sleep(0.3)
        raise PlaygroundError(503, "browser_unavailable", "Chrome debug endpoint did not come up.")

    async def __aenter__(self):
        try:
            port = free_loopback_port()
            self.chrome = await spawn("/usr/bin/google-chrome", *self.chrome_flags(port))
            endpoint = await self.wait_debug_endpoint(port)
            self.driver = await async_playwright().start()
            self.browser = await self.driver.chromium.connect_over_cdp(endpoint)
            for _ in range(40):
                if self.browser.contexts:
                    break
                await asyncio.sleep(0.25)
            if not self.browser.contexts:
                raise PlaygroundError(503, "browser_unavailable", "Chrome exposed no browser context.")
            self.context = self.browser.contexts[0]
            self.context.on("close", self.mark_closed)
            await self.context.add_init_script(STREAM_TEE_JS)
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *_):
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass
        finally:
            try:
                if self.driver:
                    await self.driver.stop()
            finally:
                await stop_process(self.chrome, 10)

    async def use_page(self, operation):
        if not self.context or self.closed:
            raise PlaygroundError(503, "browser_unavailable", "Chrome is not connected. Restart the gateway service.")
        page = None
        try:
            page = await self.context.new_page()
            return await operation(page)
        except BrowserError as exc:
            raise PlaygroundError(502, "browser_error", "AI Studio could not be read. Review it in the login window.") from exc
        finally:
            if page and not page.is_closed():
                try:
                    await page.close()
                except BrowserError:
                    pass

    async def check_session(self):
        async def inspect(page):
            await self.ui.open_chat(page)
            await self.ui.composer(page)
        await self.use_page(inspect)

    async def generate(self, prompt, emit=None, model=None):
        """Generate with the requested upstream model, or the configured list.

        A concrete model is used exactly as asked. Without one, the candidates
        are tried in order so an unavailable model does not fail the request.
        """
        candidates = (model,) if model else self.models
        started = False

        async def tracked(delta):
            nonlocal started
            started = True
            if emit:
                await emit(delta)

        last = None
        for index, candidate in enumerate(candidates):
            target = chat_url(candidate)

            async def operation(page, target=target):
                self.ui.chat = target
                return await self.ui.generate(page, prompt, tracked if emit else None)

            try:
                return await self.use_page(operation)
            except PlaygroundError as exc:
                last = exc
                # Only fall back to another model before any text was streamed
                # and only for refusals / exhausted quota / overload.
                if started or index + 1 >= len(candidates) or exc.status not in (403, 429, 503):
                    raise
        raise last
