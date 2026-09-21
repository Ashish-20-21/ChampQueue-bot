"""Shared test setup. Nothing here touches real Discord or Supabase.

config.py refuses to import without a few env vars, so we set harmless
fakes BEFORE anything imports it. create_client() in database/db.py does
not open a connection at import time, so a dummy URL is enough.
"""
import os
import sys
from pathlib import Path

for _k, _v in {
    "DISCORD_BOT_TOKEN": "test-token",
    "GUILD_ID": "1",
    "ADMIN_ROLE_IDS": "1",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_SERVICE_KEY": "test-key",
}.items():
    os.environ.setdefault(_k, _v)

# make `import config`, `import cogs.queue` work when pytest runs from anywhere
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
