import asyncio
import os
import secrets


async def stop_process(process, timeout=8):
    if process is None or process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


async def spawn(*args, env=None):
    return await asyncio.create_subprocess_exec(
        *args, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )


class Display:
    def __init__(self, runtime, state, number):
        self.authority = runtime / "Xauthority"
        self.env = dict(os.environ, DISPLAY=f":{number}", XAUTHORITY=str(self.authority), HOME=str(state))
        self.process = None

    async def __aenter__(self):
        self.authority.touch(mode=0o600, exist_ok=True)
        display = self.env["DISPLAY"]
        auth = await asyncio.create_subprocess_exec(
            "/usr/bin/xauth", "-q", "-f", str(self.authority), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await auth.communicate(f"add {display} . {secrets.token_hex(16)}\n".encode())
        if auth.returncode:
            raise RuntimeError("Could not configure the display.")
        self.process = await spawn("/usr/bin/Xvfb", display, "-screen", "0", "1440x900x24",
                                   "-nolisten", "tcp", "-auth", str(self.authority), env=self.env)
        try:
            for _ in range(45):
                if self.process.returncode is not None:
                    raise RuntimeError("Xvfb exited.")
                probe = await spawn("/usr/bin/xdpyinfo", env=self.env)
                if not await probe.wait():
                    return self.env
                await asyncio.sleep(0.1)
            raise RuntimeError("Xvfb did not start.")
        except BaseException:
            await stop_process(self.process)
            raise

    async def __aexit__(self, *_):
        await stop_process(self.process)
