import asyncio
import json
import time
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import playground
from gateway import ALIAS, UPSTREAM_MODELS, make_app
from playground import STREAM_TEE_JS, StudioUI


# The page submits with XHR, exactly like AI Studio does, so the injected tee
# is exercised on the real transport instead of a simulated one.
FIXTURE = """<!doctype html>
<textarea aria-label="Prompt"></textarea>
<button onclick="submit()">Run</button>
<script>
function submit() {
  const prompt = document.querySelector('textarea').value;
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GenerateContent');
  xhr.setRequestHeader('Content-Type', 'application/json+protobuf');
  xhr.send(prompt);
  document.querySelector('textarea').value = '';
}
</script>"""

RPC = "/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GenerateContent"


def frame(text, finish=None):
    """One response frame in AI Studio's protobuf-as-JSON shape."""
    content = [[[None, text]], "model"]
    return [[[content, finish]], None, None]


def body(*frames):
    return ")]}'\n" + json.dumps(list(frames))


class FixtureBrowser:
    def __init__(self, context, origin):
        self.context = context
        self.ui = StudioUI(origin, origin + "/chat")
        self.requested = []

    async def visit(self, fn):
        page = await self.context.new_page()
        try:
            return await fn(page)
        finally:
            await page.close()

    async def check_session(self):
        async def check(page):
            await self.ui.open_chat(page)
            await self.ui.composer(page)
        await self.visit(check)

    async def generate(self, prompt, emit=None, model=None):
        self.requested.append(model)
        return await self.visit(lambda page: self.ui.generate(page, prompt, emit))


class TestGateway(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_generation_and_retry(self):
        calls = {"flaky": 0}

        async def chat(_):
            return web.Response(text=FIXTURE, content_type="text/html")

        async def rpc(request):
            prompt = (await request.text()).strip()
            if prompt == "permission test":
                return web.Response(status=403, text='[,[7,"The caller does not have permission"]]')
            if prompt == "flaky test":
                calls["flaky"] += 1
                if calls["flaky"] == 1:
                    return web.Response(status=403, text='[,[7,"The caller does not have permission"]]')
                prompt = "hello"
            if prompt == "quota test":
                return web.Response(status=429, text='[,[8,"Quota exceeded"]]')
            response = web.StreamResponse(headers={"Content-Type": "application/json+protobuf"})
            await response.prepare(request)
            if prompt != "hello":
                await response.write(body(frame("Received " + prompt)).encode())
                await response.write_eof()
                return response
            # Two arrays wrap the frames, and the prefix appears once: the
            # real protocol. Frames arrive one at a time.
            await response.write(b")]}'\n[[")
            pieces = [frame("Received hello"), frame("\nSecond"), frame(" line")]
            for index, item in enumerate(pieces + [frame("\nDone", finish=1)]):
                if index:
                    await response.write(b",")
                await response.write(json.dumps(item).encode())
                await asyncio.sleep(0.3)
            await response.write(b"]]")
            await response.write_eof()
            return response

        fixture = web.Application()
        fixture.router.add_get("/chat", chat)
        fixture.router.add_post(RPC, rpc)
        key = "synthetic_browser_fixture_gateway_key"
        headers = {"Authorization": "Bearer " + key}

        async with TestServer(fixture) as server:
            from playwright.async_api import async_playwright

            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(channel="chrome", headless=True)
                async with browser:
                    context = await browser.new_context()
                    await context.add_init_script(STREAM_TEE_JS)
                    adapter = FixtureBrowser(context, str(server.make_url("/")).rstrip("/"))
                    async with TestClient(TestServer(make_app(adapter, key))) as proxy:
                        async with proxy.get("/v1/models") as reply:
                            self.assertEqual(reply.status, 401)
                        async with proxy.get("/v1/models", headers=headers) as reply:
                            entries = [entry["id"] for entry in (await reply.json())["data"]]
                            self.assertEqual(entries[0], ALIAS)
                            for name in UPSTREAM_MODELS:
                                self.assertIn(name, entries)
                        async with proxy.get("/v1/models/" + UPSTREAM_MODELS[0], headers=headers) as reply:
                            self.assertEqual((await reply.json())["id"], UPSTREAM_MODELS[0])
                        async with proxy.get("/status", headers=headers) as reply:
                            self.assertEqual((await reply.json())["state"], "ready")

                        request = {"model": ALIAS, "messages": [{"role": "user", "content": "hello"}]}
                        async with proxy.post("/v1/chat/completions", headers=headers, json=request) as reply:
                            self.assertEqual(reply.status, 200)
                            self.assertEqual((await reply.json())["choices"][0]["message"]["content"],
                                             "Received hello\nSecond line\nDone")

                        # A concrete model id is honoured, not replaced by the alias.
                        request["model"] = UPSTREAM_MODELS[0]
                        async with proxy.post("/v1/chat/completions", headers=headers, json=request) as reply:
                            self.assertEqual(reply.status, 200)
                            self.assertEqual(adapter.requested[-1], UPSTREAM_MODELS[0])

                        request["model"] = "not-a-real-model"
                        async with proxy.post("/v1/chat/completions", headers=headers, json=request) as reply:
                            self.assertEqual(reply.status, 400)
                        request["model"] = ALIAS

                        request["stream"] = True
                        async with proxy.post("/v1/chat/completions", headers=headers, json=request) as reply:
                            self.assertEqual(reply.content_type, "text/event-stream")
                            start, text, done = None, [], False
                            async for line in reply.content:
                                if not line.startswith(b"data: "):
                                    continue
                                if line.strip() == b"data: [DONE]":
                                    done = True
                                    break
                                item = json.loads(line[6:])["choices"][0]["delta"].get("content")
                                if item:
                                    start = start or time.monotonic()
                                    text.append(item)
                            self.assertTrue(done)
                            self.assertEqual("".join(text), "Received hello\nSecond line\nDone")
                            # The upstream sends a frame every 0.3s, so a real
                            # stream cannot deliver everything at once.
                            self.assertGreater(time.monotonic() - start, .5)

                        request["messages"][0]["content"] = "quota test"
                        async with proxy.post("/v1/chat/completions", headers=headers, json=request) as reply:
                            self.assertEqual(reply.status, 429)
                            self.assertEqual((await reply.json())["error"]["code"], "google_quota")

                        # A refused send is retried on the same warm page.
                        request["stream"] = False
                        request["messages"][0]["content"] = "flaky test"
                        async with proxy.post("/v1/chat/completions", headers=headers, json=request) as reply:
                            self.assertEqual(reply.status, 200)
                            self.assertTrue((await reply.json())["choices"][0]["message"]["content"].strip())
                            self.assertEqual(calls["flaky"], 2)

                        request["messages"][0]["content"] = "permission test"
                        previous = playground.RETRIES
                        playground.RETRIES = 2
                        try:
                            async with proxy.post("/v1/chat/completions", headers=headers, json=request) as reply:
                                self.assertEqual(reply.status, 403)
                                self.assertEqual((await reply.json())["error"]["code"], "google_permission_denied")
                        finally:
                            playground.RETRIES = previous


if __name__ == "__main__":
    unittest.main()