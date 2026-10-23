"""Add a GPT-Live delegation hint without replacing the working SDK/API setup.

Run from the zovak-backend root: python scripts/enable_voice_confirmation_prompt.py
"""
from pathlib import Path

root = Path(__file__).resolve().parents[1]
prompt_path = root / "zovak" / "providers" / "voice" / "gpt_live.py"
marker = "# ZOVAK_VOICE_CONFIRMATION_PROMPT_V1"
if not prompt_path.is_file():
    raise SystemExit(f"Not found: {prompt_path}")
source = prompt_path.read_text()
if marker in source:
    print("Voice confirmation delegation prompt already enabled.")
else:
    source += f'''

{marker}
# Add to the existing voice prompt WITHOUT changing the session creation API.
LIVE_PROMPT += (
    " After a pending appointment proposal, the backend will ask the "
    "customer politely whether they are sure. Always delegate the customer's "
    "next reply to the backend, even if the reply is short, such as yes, no, "
    "please confirm it, or change the time. Never claim a booking is confirmed "
    "unless the backend returns verified confirmation."
)
'''
    prompt_path.write_text(source)
    print("Added confirmation delegation rules to LIVE_PROMPT (preserved existing SDK code).")
