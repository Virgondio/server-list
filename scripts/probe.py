#!/usr/bin/env python3
# Probe servers/*.toml via xash3d-query, emit output/v1/servers/<gamedir>.
# State persists in <output>/state.json across runs (grace window for
# spurious failures, pruning of long-gone addresses).

import argparse
import json
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path


PROTO_XASH = 49
PROTO_GOLDSRC = 48
LIVE_STATUSES = ("ok", "okwithplayers")
BACKOFF = (0.25, 1.0, 2.0, 4.0, 4.0)
STATE_PRUNE_HOURS = 30 * 24


def probe_one(query_bin, address, timeout):
	cmd = [query_bin, "info", address, "-j", "-c", "-P", "-t", str(int(timeout))]

	try:
		out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
	except subprocess.TimeoutExpired:
		return None

	if not out.stdout.strip():
		return None

	try:
		doc = json.loads(out.stdout)
	except json.JSONDecodeError:
		return None

	servers = doc.get("servers") or []
	return servers[0] if servers else None

def probe_with_retry(query_bin, address, tries, timeout):
	for i in range(tries):
		result = probe_one(query_bin, address, timeout)
		if result is not None and result.get("status") in LIVE_STATUSES:
			return result
		if i + 1 < tries:
			time.sleep(BACKOFF[min(i, len(BACKOFF) - 1)])
	return None

def load_sources(servers_dir):
	sources = {}
	for path in sorted(servers_dir.glob("*.toml")):
		gamedir = path.stem
		with open(path, "rb") as f:
			doc = tomllib.load(f)
		entries = doc.get("server") or []
		if not entries:
			continue
		sources[gamedir] = entries
	return sources

def write_output(output_dir, gamedir, addresses):
	out_dir = output_dir / "v1" / "servers"
	out_dir.mkdir(parents=True, exist_ok=True)
	out_path = out_dir / gamedir

	lines = [
		f"# {gamedir} server list",
		f"# Generated {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
		"# Format: <ip|gs> <address>",
		"",
	]
	for addr, proto in addresses:
		directive = "gs" if proto == PROTO_GOLDSRC else "ip"
		lines.append(f"{directive} {addr}")
	lines.append("")
	out_path.write_text("\n".join(lines))
	return out_path

def load_state(path):
	if not path.exists():
		return {}
	try:
		with open(path) as f:
			return json.load(f)
	except (OSError, json.JSONDecodeError):
		return {}

def save_state(path, state):
	path.parent.mkdir(parents=True, exist_ok=True)
	with open(path, "w") as f:
		json.dump(state, f, indent=2, sort_keys=True)

def hours_since(iso, now):
	if not iso:
		return float("inf")
	try:
		dt = datetime.fromisoformat(iso)
	except ValueError:
		return float("inf")
	if dt.tzinfo is None:
		dt = dt.replace(tzinfo=timezone.utc)
	return (now - dt).total_seconds() / 3600.0

def main():
	ap = argparse.ArgumentParser(description=__doc__)
	ap.add_argument("--query", default="xash3d-query", help="path to the xash3d-query binary")
	ap.add_argument("--sources", default="servers", help="directory containing per-gamedir TOML sources")
	ap.add_argument("--output", default="output", help="directory to write the publishable tree into")
	ap.add_argument("--tries", type=int, default=4, help="probe attempts per server")
	ap.add_argument("--timeout", type=int, default=2, help="per-probe response timeout, in whole seconds (passed to xash3d-query -t)")
	ap.add_argument("--grace-hours", type=float, default=48.0, help="keep a silent server published if it responded within this many hours")
	args = ap.parse_args()

	sources_dir = Path(args.sources)
	output_dir = Path(args.output)
	state_path = output_dir / "state.json"

	if not sources_dir.is_dir():
		print(f"error: {sources_dir} does not exist", file=sys.stderr)
		return 2

	sources = load_sources(sources_dir)
	if not sources:
		print(f"error: no .toml files under {sources_dir}", file=sys.stderr)
		return 2

	state = load_state(state_path)
	now = datetime.now(timezone.utc)
	now_iso = now.replace(microsecond=0).isoformat()

	total = sum(len(v) for v in sources.values())
	live_now = 0
	grace_kept = 0
	print(f"probing {total} servers across {len(sources)} gamedirs  (tries={args.tries}, timeout={args.timeout}s, grace={args.grace_hours}h)", flush=True)

	for gamedir, entries in sources.items():
		gd_state = state.setdefault(gamedir, {})
		live_addrs = []

		for entry in entries:
			address = entry.get("address")
			if not address:
				continue
			proto = int(entry.get("protocol") or PROTO_XASH)
			prev = gd_state.setdefault(address, {})

			result = probe_with_retry(args.query, address, args.tries, args.timeout)

			prev["last_attempt"] = now_iso
			if result is not None:
				prev["last_seen"] = now_iso
				prev["last_ping_ms"] = result.get("ping")
				# use the source's protocol, not the responder's
				live_addrs.append((address, proto))
				live_now += 1
				print(f"  [+] {gamedir:>12}  {address}  ping={result.get('ping')}ms", flush=True)
				continue

			age = hours_since(prev.get("last_seen"), now)
			if age <= args.grace_hours:
				live_addrs.append((address, proto))
				grace_kept += 1
				print(f"  [~] {gamedir:>12}  {address}  silent now, last seen {age:.1f}h ago (grace)", flush=True)
			elif prev.get("last_seen"):
				print(f"  [-] {gamedir:>12}  {address}  silent (last seen {age:.1f}h ago)", flush=True)
			else:
				print(f"  [-] {gamedir:>12}  {address}", flush=True)

		# always emit a file so the URL is reachable even when 0 servers respond
		live_addrs.sort()
		out_path = write_output(output_dir, gamedir, live_addrs)
		print(f"  -> {out_path}  ({len(live_addrs)} published)", flush=True)

	source_addrs = {gd: {e["address"] for e in entries if e.get("address")} for gd, entries in sources.items()}
	pruned = 0
	for gd in list(state.keys()):
		if gd not in source_addrs:
			for addr, entry in list(state[gd].items()):
				if hours_since(entry.get("last_seen"), now) > STATE_PRUNE_HOURS:
					del state[gd][addr]
					pruned += 1
			if not state[gd]:
				del state[gd]
			continue
		for addr in list(state[gd].keys()):
			if addr in source_addrs[gd]:
				continue
			if hours_since(state[gd][addr].get("last_seen"), now) > STATE_PRUNE_HOURS:
				del state[gd][addr]
				pruned += 1

	save_state(state_path, state)

	print(f"done: {live_now} responded, {grace_kept} kept by grace, {pruned} pruned from state", flush=True)
	return 0


if __name__ == "__main__":
	sys.exit(main())
