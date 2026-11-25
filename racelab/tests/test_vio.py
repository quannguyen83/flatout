"""Dataset handling and local receiver lifecycle without hardware."""
import asyncio
import json
import sys

from racelab.api.vio import Vio

def test_config_and_renamed_recording(tmp_path):
    async def main():
        vio = Vio()
        try:
            script = tmp_path / "laptop_receiver.py"
            script.touch()
            session = tmp_path / "captures" / "session1"
            session.mkdir(parents=True)
            video = session / "video_1280x720_30fps.mkv"
            video.write_bytes(b"video")
            (session / "summary.json").write_text(json.dumps({"video_file": video.name}))
            state = await vio.action("receiver-config", {"file": str(script), "args": "--output captures"})
            assert state["datasets"] == ["session1"]
            assert vio.video("session1") == video
        finally:
            await vio.close()
    asyncio.run(main())

def test_local_receiver_graceful_stop(tmp_path):
    async def main():
        vio = Vio()
        script = tmp_path / "laptop_receiver.py"
        script.write_text(
            "import signal, time, pathlib\n"
            "done = False\n"
            "def stop(*args):\n global done\n done = True\n"
            "signal.signal(signal.SIGINT, stop)\n"
            "print('Ready', flush=True)\n"
            "while not done: time.sleep(.05)\n"
            "pathlib.Path('finished').touch()\n"
        )
        try:
            await vio.action("receiver-start", {"file": str(script), "python": sys.executable})
            for _ in range(100):
                if "Ready" in vio.logs["receiver"]:
                    break
                await asyncio.sleep(.05)
            assert "Ready" in vio.logs["receiver"]
            await vio.action("receiver-stop", {})
            assert (tmp_path / "finished").exists()
            assert not vio.local_running()
        finally:
            await vio.close()
    asyncio.run(main())
