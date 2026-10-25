"""
Purpose: Preserve the existing Foundry operator command path.
Input: Arguments for the Foundry backend command.
Output: The result from the command under backend/foundry.
"""
from pathlib import Path
import runpy


if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).parent / "foundry" / "provision.py"),
                  run_name="__main__")
