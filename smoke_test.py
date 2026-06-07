"""End-to-end smoke test against the live Reaper bridge."""

import json
import subprocess
import sys

from reaper_mcp.bridge import BridgeClient
from reaper_mcp.server import _discover_installed_fx

b = BridgeClient()

print("--- ping ---")
print(json.dumps(b.call("ping"), indent=2))

print()
print("--- get_project_info ---")
print(json.dumps(b.call("get_project_info"), indent=2))

print()
print("--- list_tracks ---")
print(json.dumps(b.call("list_tracks"), indent=2))

print()
print("--- list_installed_fx (host-side, all kinds) ---")
fx = _discover_installed_fx()
print(f"total scanned: {len(fx)}")
print("by kind:")
kinds: dict[str, int] = {}
for it in fx:
    kinds[it["kind"]] = kinds.get(it["kind"], 0) + 1
for k, n in sorted(kinds.items()):
    print(f"  {k}: {n}")

print()
print("--- instruments only (first 10) ---")
for it in [x for x in fx if x["is_instrument"]][:10]:
    print(" ", it["name"])
