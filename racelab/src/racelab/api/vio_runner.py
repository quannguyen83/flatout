"""Run the existing local receiver and translate a stdin stop request to SIGINT."""
import _thread
import runpy
import sys
import threading
from pathlib import Path

def main():
    script = Path(sys.argv[1]).resolve()
    sys.argv = [str(script), *sys.argv[2:]]
    sys.path.insert(0, str(script.parent))
    def watch():
        # EOF also stops capture if RaceLab exits.
        sys.stdin.readline()
        _thread.interrupt_main()
    threading.Thread(target=watch, daemon=True).start()
    runpy.run_path(str(script), run_name="__main__")

if __name__ == "__main__":
    main()
