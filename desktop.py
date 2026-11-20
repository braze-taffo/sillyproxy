"""Opens a temporary, local-only remote desktop for interactive Google login."""
import asyncio
import os
from pathlib import Path
import secrets
import signal
import string

from display import Display, spawn, stop_process


async def login():
    runtime = Path(os.environ["RUNTIME_DIRECTORY"])
    state = Path(os.environ["STATE_DIRECTORY"])
    password = runtime / "vnc-password"
    value = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
    descriptor = os.open(password, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(value + "\n")
    chrome = vnc = proxy = None
    async with Display(runtime, state, 188) as env:
        try:
            chrome = await spawn(
                "/usr/bin/google-chrome",
                f"--user-data-dir={state / 'chrome-profile'}",
                "--profile-directory=Default",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-dev-shm-usage",
                "--disable-features=SignInProfileCreation",
                "--window-size=1440,900",
                "--password-store=basic",
                "https://aistudio.google.com/prompts/new_chat?temporary=true",
                env=env,
            )
            vnc = await spawn("/usr/bin/x11vnc", "-display", env["DISPLAY"], "-auth", env["XAUTHORITY"],
                              "-no6", "-listen", "127.0.0.1", "-rfbport", "15900", "-passwdfile", str(password),
                              "-forever", env=env)
            proxy = await spawn("/usr/bin/websockify", "--web", "/usr/share/novnc", "127.0.0.1:16080", "127.0.0.1:15900")
            children = [asyncio.create_task(process.wait()) for process in (chrome, vnc, proxy)]
            try:
                finished, _ = await asyncio.wait(children, return_when=asyncio.FIRST_COMPLETED)
                if any(child.result() for child in finished):
                    raise RuntimeError("Chrome or the temporary desktop stopped unexpectedly.")
            finally:
                for child in children:
                    child.cancel()
        finally:
            await stop_process(proxy)
            await stop_process(vnc)
            await stop_process(chrome, 15)


async def main():
    task = asyncio.create_task(login())
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    asyncio.run(main())
