#!/usr/bin/env python3
"""
Bittensor Automated Event-Driven Staking Bot (WS-first, Any-Subnet, Production-optimized)

Objective:
- Monitor every new block with minimal latency
- Count StakeAdded events per subnet (netuid) in each block
- If any subnet in block N has >= 2 StakeAdded events -> submit stake to that subnet ASAP (best-effort N/N+1)
- Schedule unstake for N+1 on that same subnet and execute promptly
- Repeat reliably with robust reconnects, concurrency, and fallbacks

Key Architecture:
- Websocket-first:
  - Subscribe to new heads (best effort earliest visibility)
  - Optional: Subscribe to System.Events (storage) when available (future enhancement)
- Separate websocket clients:
  - Client A: headers
  - Client B: events (dedicated, used for get_events per-block; upgradeable to subscribe storage)
  - Client C: extrinsics
- Fallbacks:
  - Iterator mode headers
  - Polling get_current_block at low latency
- Nonce/priority:
  - Explicit NonceManager (optimistic increment, refresh on mismatch)
  - Optional tip for priority
  - No waits on inclusion/finality (fast path)

Run:
    python stake_bot.py

Config:
    config.yaml is created on first run. Edit:
    - network
    - optional chain_endpoint (single) or leave empty to use defaults
    - wallet_name, hotkey_name, wallet_password
    - target_hotkey (validator hotkey SS58 on target subnet) or leave empty to stake to your own hotkey
    - subnet_mode: "any" (stake on any subnet that triggers) or "single"
    - subnet_id (used only when subnet_mode == "single")
    - stake_amount (TAO)
    - fast_mode, tip_tao, wait_for_inclusion/finalization
    - subscription_mode: auto | callback | poll
    - header_wait_timeout_seconds
    - poll_interval_seconds
    - log_level, log_to_file, log_file
    - persist_state, state_file
"""

import os
import sys
import time
import json
import signal
import logging
from logging.handlers import RotatingFileHandler
from typing import Optional, Dict, Any, List, Tuple, Set
import inspect
import queue
import threading

import bittensor as bt

# Optional YAML dependency with minimal fallback parser
try:
    import yaml  # PyYAML
    YAML_AVAILABLE = True
except Exception:
    YAML_AVAILABLE = False


DEFAULT_CONFIG = {
    # Network and optional endpoint
    "network": "finney",                 # "finney" (mainnet) or "test"
    "chain_endpoint": "",                # optional ws endpoint (wss://host:port). Leave empty to use defaults

    # Wallet & accounting
    "wallet_name": "default",
    "hotkey_name": "default",
    "wallet_password": "",               # optional: set to avoid repeated prompt per run
    "target_hotkey": "",                 # optional: stake to this hotkey (validator) if set; otherwise your wallet's hotkey
    "stake_amount": 0.05,                # TAO

    # Subnet targeting
    "subnet_mode": "any",                # "any" -> trigger on any subnet; "single" -> only on subnet_id
    "subnet_id": 63,                     # used only when subnet_mode == "single"

    # Performance & behavior
    "fast_mode": True,                   # explicit nonce & tip, zero-wait extrinsics
    "tip_tao": 0.0,                      # optional tip to improve priority
    "wait_for_inclusion": False,         # False = faster, True = wait for inclusion
    "wait_for_finalization": False,      # False = fastest, True = wait for finalization

    # Retries/backoff
    "max_retries": 5,
    "retry_backoff_seconds": 0.5,        # starting backoff
    "max_backoff_seconds": 4.0,

    # Websocket reconnects
    "ws_reconnect_backoff_seconds": 1.0,
    "ws_reconnect_max_backoff_seconds": 8.0,

    # Subscription strategy
    "subscription_mode": "auto",         # auto | callback | poll
    "header_wait_timeout_seconds": 3.0,  # fallback if no headers in this time (since ~12s blocks, 2-3s is aggressive)
    "poll_interval_seconds": 0.5,        # polling interval when in poll mode

    # Submission behavior
    "no_sdk_fallback": True,             # if True, do not fallback to SDK path; only use fast extrinsic
    "unstake_signer": "auto",            # 'coldkey' | 'hotkey' | 'auto': which key signs unstake; auto tries coldkey then hotkey

    # Logging
    "log_level": "INFO",                 # DEBUG | INFO | WARNING | ERROR
    "log_to_file": True,
    "log_file": "stake_bot.log",

    # State persistence
    "persist_state": True,
    "state_file": "stake_state.json",

    # Event export per block
    "export_events": True,               # write JSON file with all block events for every block
    "events_dir": "block_events",        # directory to write per-block JSON files

    # Trigger safety
    "allow_overlap_triggers": False,     # if False: don't trigger stake while an unstake is pending per-subnet
    "block_new_stakes_while_pending_unstake": True  # if True: globally block all new stakes until all pending unstakes execute
}

CONFIG_PATH = os.environ.get("STAKE_BOT_CONFIG", "config.yaml")


def _simple_parse_yaml(path: str) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    try:
        with open(path, "r") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" not in line:
                    continue
                key, val = line.split(":", 1)
                key = key.strip()
                val = val.strip()
                if " #" in val:
                    val = val.split(" #", 1)[0].strip()
                low = val.lower()
                if low in ("true", "false"):
                    data[key] = (low == "true")
                    continue
                try:
                    if "." in val:
                        data[key] = float(val)
                    else:
                        data[key] = int(val)
                    continue
                except Exception:
                    pass
                if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                data[key] = val
    except Exception:
        return {}
    return data


def _simple_dump_yaml(path: str, obj: Dict[str, Any]) -> None:
    lines = [
        "# Event-Driven Staking Bot Configuration",
        "# Edit values as needed. This file is read at startup by stake_bot.py",
        ""
    ]
    for k, v in obj.items():
        if isinstance(v, bool):
            sval = "true" if v else "false"
        else:
            sval = str(v)
        lines.append(f"{k}: {sval}")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


class NonceManager:
    """Local nonce manager with refresh on mismatch."""
    def __init__(self, substrate, signer_ss58: str, logger: logging.Logger):
        self.substrate = substrate
        self.signer_ss58 = signer_ss58
        self.logger = logger
        self._cached: Optional[int] = None

    def refresh(self) -> int:
        try:
            self._cached = int(self.substrate.get_account_nonce(self.signer_ss58))
            self.logger.debug("Nonce refresh -> %d", self._cached)
        except Exception as e:
            self.logger.warning("Failed to fetch nonce for %s: %s", self.signer_ss58, str(e))
            self._cached = None
        return self._cached if self._cached is not None else 0

    def current(self) -> int:
        if self._cached is None:
            return self.refresh()
        return self._cached

    def next_and_increment(self) -> int:
        if self._cached is None:
            self.refresh()
        if self._cached is None:
            self._cached = 0
        nonce = self._cached
        self._cached += 1
        return nonce

    def invalidate(self):
        self._cached = None


BOT = None  # Global so module-level functions can delegate


class AutoStakeBot:
    def __init__(self, config: Dict[str, Any]):
        self.cfg = config
        self._setup_logging()
        self.logger = logging.getLogger("stake_bot")

        # Config
        self.network = self.cfg["network"]
        self.chain_endpoint = str(self.cfg.get("chain_endpoint", "")).strip()

        self.wallet_name = self.cfg["wallet_name"]
        self.hotkey_name = self.cfg["hotkey_name"]
        # If set, stake/unstake is directed to this hotkey (delegation); otherwise your own wallet.hotkey
        self.target_hotkey = str(self.cfg.get("target_hotkey", "")).strip()

        self.subnet_mode = str(self.cfg.get("subnet_mode", "any")).lower()
        self.subnet_id = int(self.cfg.get("subnet_id", 63))
        self.stake_amount_tao = float(self.cfg["stake_amount"])

        self.fast_mode = bool(self.cfg.get("fast_mode", True))
        self.tip_tao = float(self.cfg.get("tip_tao", 0.0))
        self.wait_for_inclusion = bool(self.cfg.get("wait_for_inclusion", False))
        self.wait_for_finalization = bool(self.cfg.get("wait_for_finalization", False))

        self.subscription_mode = str(self.cfg.get("subscription_mode", "auto")).lower()
        self.header_timeout = float(self.cfg.get("header_wait_timeout_seconds", 3.0))
        self.poll_interval = float(self.cfg.get("poll_interval_seconds", 0.5))
        self.no_sdk_fallback = bool(self.cfg.get("no_sdk_fallback", True))
        self.unstake_signer = str(self.cfg.get("unstake_signer", "auto")).lower()
        # Event export
        self.export_events = bool(self.cfg.get("export_events", True))
        self.events_dir = str(self.cfg.get("events_dir", "block_events"))

        # Global gating
        self.block_new_stakes_globally = bool(self.cfg.get("block_new_stakes_while_pending_unstake", True))

        # Clients
        self.subtensor: Optional[bt.Subtensor] = None         # primary client (headers)
        self.substrate = None
        self.events_subtensor: Optional[bt.Subtensor] = None  # dedicated events client
        self.substrate_events = None
        self.tx_subtensor: Optional[bt.Subtensor] = None      # dedicated extrinsic client
        self.substrate_tx = None

        # Wallet & Nonce
        self.wallet: Optional[Any] = None
        self.nonce_mgr: Optional[NonceManager] = None
        self.nonce_mgr_hot: Optional[NonceManager] = None

        # State (trigger/unstake coordination) per subnet
        self.last_analyzed_block: Optional[int] = None
        self.last_staked_block_map: Dict[int, int] = {}        # netuid -> last staked block
        self.pending_unstake_blocks: Dict[int, int] = {}       # netuid -> scheduled unstake block
        self.alpha_added_last_block: Dict[int, int] = {}       # netuid -> alpha amount from our last stake event
        # Legacy single fields kept for migration
        self._legacy_last_staked_block: Optional[int] = None
        self._legacy_pending_unstake_block: Optional[int] = None

        # Runtime call discovery/caching
        self.call_pallet: Optional[str] = None
        self.stake_call_candidates: List[str] = ["add_stake"]
        self.unstake_call_candidates: List[str] = ["remove_stake_full_limit", "remove_stake"]

        # Concurrency/threading
        self._shutdown = False
        
        # Rate limiting: Track last operation time
        self.last_operation_time: float = 0.0  # Unix timestamp of last stake/unstake

    def _setup_logging(self):
        level = getattr(logging, str(self.cfg.get("log_level", "INFO")).upper(), logging.INFO)
        logging.basicConfig(
            level=level,
            format="%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            handlers=[logging.StreamHandler(sys.stdout)]
        )
        if self.cfg.get("log_to_file", True):
            handler = RotatingFileHandler(
                self.cfg.get("log_file", "stake_bot.log"),
                maxBytes=5 * 1024 * 1024,
                backupCount=3
            )
            handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
            logging.getLogger().addHandler(handler)

    def _load_state(self):
        if not self.cfg.get("persist_state", True):
            return
        path = self.cfg.get("state_file", "stake_state.json")
        try:
            if os.path.exists(path):
                with open(path, "r") as f:
                    s = json.load(f)
                # New format - ensure keys are integers
                raw_staked = s.get("last_staked_block_map", {}) or {}
                raw_pending = s.get("pending_unstake_blocks", {}) or {}
                self.last_staked_block_map = {int(k): v for k, v in raw_staked.items()}
                self.pending_unstake_blocks = {int(k): v for k, v in raw_pending.items()}
                # Legacy migration
                self._legacy_last_staked_block = s.get("last_staked_block")
                self._legacy_pending_unstake_block = s.get("pending_unstake_block")
                migrated = False
                if self._legacy_last_staked_block is not None and self.subnet_mode == "single":
                    self.last_staked_block_map[self.subnet_id] = int(self._legacy_last_staked_block)
                    migrated = True
                if self._legacy_pending_unstake_block is not None and self.subnet_mode == "single":
                    self.pending_unstake_blocks[self.subnet_id] = int(self._legacy_pending_unstake_block)
                    migrated = True
                if migrated:
                    self.logger.info("Migrated legacy state to per-subnet state maps.")
                self.logger.info("Restored state: last_staked_block_map=%s pending_unstake_blocks=%s",
                                 self.last_staked_block_map, self.pending_unstake_blocks)
        except Exception as e:
            self.logger.warning("Failed to load state: %s", str(e))

    def _save_state(self):
        if not self.cfg.get("persist_state", True):
            return
        try:
            s = {
                "last_staked_block_map": self.last_staked_block_map,
                "pending_unstake_blocks": self.pending_unstake_blocks,
                # legacy fields kept for back-compat with older versions
                "last_staked_block": self._legacy_last_staked_block,
                "pending_unstake_block": self._legacy_pending_unstake_block
            }
            with open(self.cfg.get("state_file", "stake_state.json"), "w") as f:
                json.dump(s, f)
        except Exception as e:
            self.logger.warning("Failed to save state: %s", str(e))

    def _new_subtensor(self) -> bt.Subtensor:
        if self.chain_endpoint:
            return bt.Subtensor(network=self.network, chain_endpoint=self.chain_endpoint)
        return bt.Subtensor(network=self.network)

    def initialize(self):
        self.logger.info("Initializing wallet and network connection...")

        # Wallet creation
        wallet_factory = getattr(bt, "wallet", None)
        if callable(wallet_factory):
            self.wallet = wallet_factory(name=self.wallet_name, hotkey=self.hotkey_name)
        else:
            from bittensor.wallet import Wallet as WalletCls  # may fail on older SDKs
            self.wallet = WalletCls(name=self.wallet_name, hotkey=self.hotkey_name)

        self.wallet.create_if_non_existent()

        # Unlock once per process
        try:
            pw = str(self.cfg.get("wallet_password", "")).strip()
            if pw:
                os.environ["BT_WALLET_PASSWORD"] = pw
            try:
                self.wallet.unlock_coldkey()
                self.logger.info("Wallet unlocked")
            except Exception:
                import getpass
                self.logger.info("Wallet unlock needs password; prompting once...")
                pw_input = getpass.getpass("Wallet password: ")
                if pw_input:
                    os.environ["BT_WALLET_PASSWORD"] = pw_input
                self.wallet.unlock_coldkey()
                self.logger.info("Wallet unlocked")
        except Exception as e:
            self.logger.info("Wallet loaded (unencrypted or already unlocked): %s", str(e))

        # Clients
        if self.chain_endpoint:
            self.logger.info("Using custom chain endpoint: %s", self.chain_endpoint)
        self.subtensor = self._new_subtensor()
        self.substrate = self.subtensor.substrate

        # Separate events client
        try:
            self.events_subtensor = self._new_subtensor()
            self.substrate_events = self.events_subtensor.substrate
        except Exception as e:
            self.logger.warning("Failed to initialize separate events substrate: %s", str(e))
            self.substrate_events = self.substrate

        # Separate extrinsic client
        try:
            self.tx_subtensor = self._new_subtensor()
            self.substrate_tx = self.tx_subtensor.substrate
        except Exception as e:
            self.logger.warning("Failed to initialize separate tx substrate: %s", str(e))
            self.substrate_tx = self.substrate

        # Discover canonical pallet and available call names
        try:
            self._discover_call_endpoints()
        except Exception as e:
            self.logger.warning("Call discovery failed (will use defaults): %s", str(e))

        self.logger.info("Connected to network: %s", self.network)

        # Nonce manager (signer is coldkey)
        signer_ss58 = self.wallet.coldkeypub.ss58_address
        self.nonce_mgr = NonceManager(self.substrate_tx, signer_ss58, self.logger)
        self.nonce_mgr_hot = NonceManager(self.substrate_tx, self.wallet.hotkey.ss58_address, self.logger)

        # Balance check
        bal = self.subtensor.get_balance(signer_ss58)
        self.logger.info("Coldkey: %s | Hotkey: %s | Balance: %s TAO",
                         signer_ss58, self.wallet.hotkey.ss58_address, bal.tao)
        if bal.tao < self.stake_amount_tao:
            raise RuntimeError(f"Insufficient balance {bal.tao} < required {self.stake_amount_tao} TAO")

        # Load persisted schedule
        self._load_state()

        # Ensure events export dir exists if enabled
        if self.export_events:
            try:
                os.makedirs(self.events_dir, exist_ok=True)
            except Exception as e:
                self.logger.warning("Failed to create events_dir '%s': %s", self.events_dir, str(e))

    def _ensure_events_client(self):
        if self.substrate_events is None:
            try:
                self.events_subtensor = self._new_subtensor()
                self.substrate_events = self.events_subtensor.substrate
            except Exception as e:
                self.logger.warning("Recreating events substrate failed: %s", str(e))
                self.substrate_events = self.substrate

    def _discover_call_endpoints(self):
        """Discover canonical pallet and available call names from runtime metadata."""
        modules = ["SubtensorModule", "Subtensor", "subtensorModule", "subtensor"]
        chosen = None
        for m in modules:
            try:
                # probe a known call to verify pallet presence
                self.substrate_tx.get_call_index(m, "add_stake")
                chosen = m
                break
            except Exception:
                continue
        self.call_pallet = chosen or "SubtensorModule"

        filtered_stake: List[str] = []
        for fn in self.stake_call_candidates:
            try:
                self.substrate_tx.get_call_index(self.call_pallet, fn)
                filtered_stake.append(fn)
            except Exception:
                continue
        if filtered_stake:
            self.stake_call_candidates = filtered_stake

        filtered_unstake: List[str] = []
        for fn in self.unstake_call_candidates:
            try:
                self.substrate_tx.get_call_index(self.call_pallet, fn)
                filtered_unstake.append(fn)
            except Exception:
                continue
        if filtered_unstake:
            self.unstake_call_candidates = filtered_unstake

        self.logger.info(
            "Using pallet '%s' with stake calls=%s unstake calls=%s",
            self.call_pallet, self.stake_call_candidates, self.unstake_call_candidates
        )

    def _events_for_block(self, block_number: int):
        """Fetch events for a given block via dedicated events client. Returns list or []"""
        self._ensure_events_client()
        try:
            block_hash = self.substrate_events.get_block_hash(block_number)
            if block_hash is None:
                return []
            events = self.substrate_events.get_events(block_hash=block_hash)
            return events or []
        except Exception as e:
            es = str(e)
            if "cannot call recv while another thread is already running recv" in es or "recv_streaming" in es:
                self.logger.warning("Events client recv conflict; recreating events substrate and retrying...")
                try:
                    self.events_subtensor = self._new_subtensor()
                    self.substrate_events = self.events_subtensor.substrate
                    block_hash = self.substrate_events.get_block_hash(block_number)
                    if block_hash is None:
                        return []
                    events = self.substrate_events.get_events(block_hash=block_hash)
                    return events or []
                except Exception as e2:
                    self.logger.warning("Retry failed for block %s: %s", block_number, str(e2))
                    return []
            self.logger.warning("Failed to fetch events for block %s: %s", block_number, es)
            return []

    def _normalize_events(self, raw_events) -> List[Dict[str, Any]]:
        """Normalize substrate-interface EventRecords into JSON-friendly dicts."""
        out: List[Dict[str, Any]] = []
        for ev in raw_events or []:
            try:
                evval = ev.value if hasattr(ev, "value") else ev
                evt = evval.get("event", {}) if isinstance(evval, dict) else {}
                module_id = evt.get("module_id") or evt.get("pallet") or evt.get("module") or ""
                event_id = evt.get("event_id") or evt.get("event") or evt.get("name") or ""

                # phase may be in top-level or under event
                phase = evval.get("phase") if isinstance(evval, dict) else None
                if isinstance(phase, dict):
                    # e.g., {'ApplyExtrinsic': 77} or {'Finalization': None}
                    phase_type = next(iter(phase.keys()), None)
                    phase_value = phase.get(phase_type)
                    phase_str = phase_type
                    extrinsic_idx = phase_value if isinstance(phase_value, int) else None
                else:
                    phase_str = str(phase) if phase is not None else None
                    extrinsic_idx = None

                # parameters under multiple keys
                params = (
                    evt.get("attributes")
                    or evt.get("params")
                    or evt.get("data")
                    or evt.get("args")
                    or []
                )
                norm_params: List[Dict[str, Any]] = []
                for p in params:
                    if isinstance(p, dict):
                        # keep only name/value
                        norm_params.append({"name": p.get("name", ""), "value": p.get("value")})
                    else:
                        norm_params.append({"name": "", "value": p})

                out.append({
                    "module": module_id,
                    "event": event_id,
                    "phase": phase_str,
                    "extrinsic_index": extrinsic_idx,
                    "params": norm_params
                })
            except Exception as e:
                out.append({"error": f"normalize_failed: {str(e)}"})
        return out

    def _export_block_events(self, block_number: int, events) -> None:
        """Write per-block JSON file with normalized events."""
        if not self.export_events:
            return
        try:
            block_hash = self.substrate_events.get_block_hash(block_number)
        except Exception:
            block_hash = None
        try:
            normalized = self._normalize_events(events)
            payload = {
                "block_number": block_number,
                "block_hash": block_hash,
                "timestamp": int(time.time()),
                "events": normalized
            }
            path = os.path.join(self.events_dir, f"{block_number}.json")
            with open(path, "w") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            self.logger.warning("Failed to export events for block %d: %s", block_number, str(e))

    def _count_event_by_subnet(self, events, event_names: Set[str]) -> Dict[int, int]:
        """Return {netuid: count} for any of event_names in provided events."""
        counts: Dict[int, int] = {}

        def _coerce_int(val) -> Optional[int]:
            if isinstance(val, int):
                return val
            if isinstance(val, str):
                try:
                    if val.lower().startswith("0x"):
                        return int(val, 16)
                    return int(val)
                except Exception:
                    return None
            return None

        for ev in events or []:
            try:
                evval = ev.value if hasattr(ev, "value") else ev
                evt = evval.get("event", {}) if isinstance(evval, dict) else {}
                module_id = evt.get("module_id") or evt.get("pallet") or evt.get("module") or ""
                event_id = evt.get("event_id") or evt.get("event") or evt.get("name") or ""
                if module_id not in {"SubtensorModule", "Subtensor"} or event_id not in event_names:
                    continue

                params = (
                    evt.get("attributes")
                    or evt.get("params")
                    or evt.get("data")
                    or evt.get("args")
                    or []
                )

                ev_netuid: Optional[int] = None
                norm_params: List[Dict[str, Any]] = []
                for p in params:
                    if isinstance(p, dict):
                        norm_params.append(p)
                    else:
                        norm_params.append({"name": "", "value": p})

                # Prefer explicit named 'netuid'
                for p in norm_params:
                    name = (p.get("name") or "").lower()
                    if name == "netuid":
                        v = _coerce_int(p.get("value"))
                        if v is not None:
                            ev_netuid = v
                            break

                # Fallback: choose last small int
                if ev_netuid is None:
                    candidates: List[int] = []
                    for p in norm_params:
                        v = _coerce_int(p.get("value"))
                        if v is not None and 0 <= v <= 65535:
                            candidates.append(v)
                    if candidates:
                        ev_netuid = candidates[-1]

                if ev_netuid is not None:
                    counts[ev_netuid] = counts.get(ev_netuid, 0) + 1
            except Exception:
                continue

        return counts

    def _count_stake_added_by_subnet(self, events) -> Dict[int, int]:
        """Return {netuid: count} of StakeAdded in provided events (robust parsing across SDK variants)."""
        return self._count_event_by_subnet(events, {"StakeAdded"})

    def _count_stake_removed_by_subnet(self, events) -> Dict[int, int]:
        """Return {netuid: count} of StakeRemoved in provided events (for diagnostics)."""
        return self._count_event_by_subnet(events, {"StakeRemoved"})

    def _update_last_alpha_from_events(self, events) -> None:
        """
        Scan events for our own StakeAdded and record the alpha amount per netuid.
        Event ordering observed: (coldkey, hotkey, tao, alpha, netuid, fee)
        """
        try:
            if not events:
                return
            my_cold = self.wallet.coldkeypub.ss58_address if self.wallet and self.wallet.coldkeypub else None
            my_hot = self.target_hotkey if self.target_hotkey else (self.wallet.hotkey.ss58_address if self.wallet and self.wallet.hotkey else None)
            if not my_cold and not my_hot:
                return

            def _coerce_int(val):
                if isinstance(val, int):
                    return val
                if isinstance(val, str):
                    try:
                        if val.lower().startswith("0x"):
                            return int(val, 16)
                        return int(val)
                    except Exception:
                        return None
                return None

            for ev in events:
                try:
                    evval = ev.value if hasattr(ev, "value") else ev
                    evt = evval.get("event", {}) if isinstance(evval, dict) else {}
                    module_id = evt.get("module_id") or evt.get("pallet") or evt.get("module") or ""
                    event_id = evt.get("event_id") or evt.get("event") or evt.get("name") or ""
                    if module_id not in {"SubtensorModule", "Subtensor"} or event_id != "StakeAdded":
                        continue

                    params = (
                        evt.get("attributes")
                        or evt.get("params")
                        or evt.get("data")
                        or evt.get("args")
                        or []
                    )
                    # Normalize to plain list of values in order
                    values = []
                    for p in params:
                        if isinstance(p, dict) and "value" in p:
                            values.append(p["value"])
                        else:
                            values.append(p)

                    # Guard for expected minimum length
                    if len(values) < 6:
                        continue

                    cold_ss58 = str(values[0])
                    hot_ss58 = str(values[1])
                    tao_val = _coerce_int(values[2])
                    alpha_val = _coerce_int(values[3])
                    netuid_val = _coerce_int(values[4])

                    # Match our own event by cold/hot where possible
                    match = False
                    if my_cold and cold_ss58 == my_cold:
                        match = True
                    if my_hot and hot_ss58 == my_hot:
                        match = True

                    if match and netuid_val is not None and alpha_val is not None and alpha_val > 0:
                        self.alpha_added_last_block[int(netuid_val)] = int(alpha_val)
                        self.logger.debug("Captured alpha from StakeAdded: netuid=%s alpha=%s", netuid_val, alpha_val)
                except Exception:
                    continue
        except Exception as e:
            self.logger.debug("Failed to update alpha from events: %s", str(e))

    def _best_effort_tip_rao(self) -> Optional[int]:
        if self.tip_tao <= 0:
            return None
        try:
            return int(bt.Balance.from_tao(self.tip_tao).rao)
        except Exception:
            return None

    def _select_signer(self, which: str):
        try:
            if which == "hotkey":
                return self.wallet.hotkey
            return self.wallet.coldkey
        except Exception:
            return self.wallet.coldkey

    def _nonce_mgr_for(self, signer_keypair) -> Optional[NonceManager]:
        try:
            addr = getattr(signer_keypair, "ss58_address", None)
            if addr == (self.wallet.coldkeypub.ss58_address if self.wallet and self.wallet.coldkeypub else None):
                return self.nonce_mgr
            if addr == (self.wallet.hotkey.ss58_address if self.wallet and self.wallet.hotkey else None):
                return getattr(self, "nonce_mgr_hot", None)
        except Exception:
            pass
        return self.nonce_mgr

    def _get_call_args(self, call_module: str, call_name: str) -> Optional[List[str]]:
        """Return ordered argument names for a call from runtime metadata."""
        try:
            meta = self.substrate_tx.get_metadata_call_function(call_module, call_name)
            args = (meta or {}).get("args") or []
            # Fallback: try alternate pallet aliases if no args returned
            if not args:
                for alt in ["SubtensorModule", "Subtensor", "subtensorModule", "subtensor"]:
                    if alt == call_module:
                        continue
                    try:
                        alt_meta = self.substrate_tx.get_metadata_call_function(alt, call_name)
                        alt_args = (alt_meta or {}).get("args") or []
                        if alt_args:
                            self.logger.debug("Resolved metadata args for %s using alternate pallet '%s'", call_name, alt)
                            args = alt_args
                            break
                    except Exception:
                        continue
            names: List[str] = []
            for a in args:
                n = a.get("name")
                if isinstance(n, str):
                    names.append(n)
            return names if names else None
        except Exception as e:
            self.logger.debug("get_metadata_call_function failed for %s.%s: %s", call_module, call_name, str(e))
            return None

    def _build_params_for_call(self, call_module: str, call_name: str, provided: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Build a params dict matching the expected argument names for (pallet.fn) using provided values,
        trying common synonyms across runtime versions.
        """
        expected = self._get_call_args(call_module, call_name)
        self.logger.debug("Metadata args for %s.%s: %s", call_module, call_name, expected)
        if not expected:
            # Fall back to provided as-is if metadata is not accessible
            return provided

        # synonym map per logical field (keys must match expected arg names from metadata)
        synonyms: Dict[str, List[str]] = {
            # Stake amount field can be named 'amount_staked' on-chain
            "amount_staked": ["amount_staked", "amount", "stake_to_be_added", "stake_to_add", "stake", "value"],
            # Some runtimes may still use 'amount'
            "amount": ["amount", "amount_staked", "stake_to_be_added", "stake_to_add", "stake", "value"],
            # Unstake amount field can be named 'amount_unstaked' on-chain
            "amount_unstaked": ["amount_unstaked", "alpha_unstaked", "amount", "unstake_amount", "value"],
            # Older codepaths/users may provide 'alpha_unstaked'
            "alpha_unstaked": ["alpha_unstaked", "amount_unstaked", "amount", "unstake_amount", "value"],
            # Hotkey and netuid are relatively stable
            "hotkey": ["hotkey", "who", "account", "hotKey"],
            "netuid": ["netuid", "netUid", "net_uid", "subnet_id", "netUidId"],
        }

        # Inverse lookup from provided keys lowercased
        provided_lc = {k.lower(): k for k in provided.keys()}

        resolved: Dict[str, Any] = {}
        for exp in expected:
            exp_lc = exp.lower()
            # Direct name match
            if exp in provided:
                resolved[exp] = provided[exp]
                continue
            if exp_lc in provided_lc:
                orig = provided_lc[exp_lc]
                resolved[exp] = provided[orig]
                continue

            # Try synonyms
            tried = synonyms.get(exp_lc, [])
            found = False
            for syn in tried:
                syn_lc = syn.lower()
                if syn in provided:
                    resolved[exp] = provided[syn]
                    found = True
                    break
                if syn_lc in provided_lc:
                    orig = provided_lc[syn_lc]
                    resolved[exp] = provided[orig]
                    found = True
                    break
            if not found:
                # Cannot satisfy required parameter
                return None

        return resolved

    def _submit_extrinsic_fast(self, call_module: str, call_function_candidates: List[str], params: Dict[str, Any], signer_keypair) -> Optional[str]:
        """Submit extrinsic via dedicated tx substrate with explicit nonce & tip (no waits)."""
        try:
            call = None
            func_used = None
            last_err = None
            for func in call_function_candidates:
                try:
                    # Build params matching metadata-expected arg names for this call
                    built = self._build_params_for_call(call_module, func, params)
                    if not built:
                        continue
                    call = self.substrate_tx.compose_call(
                        call_module=call_module,
                        call_function=func,
                        call_params=built
                    )
                    func_used = func
                    break
                except Exception as e_comp:
                    last_err = e_comp
                    continue
            if call is None:
                self.logger.debug("Fast mode: no matching function in %s among %s", call_module, call_function_candidates)
                if last_err:
                    self.logger.debug("Last compose_call error: %s", str(last_err))
                return None

            nm = self._nonce_mgr_for(signer_keypair)
            nonce = nm.next_and_increment() if nm else None
            tip_rao = self._best_effort_tip_rao()

            extrinsic = self.substrate_tx.create_signed_extrinsic(
                call=call,
                keypair=signer_keypair,
                tip=tip_rao if tip_rao is not None else 0,
                nonce=nonce
            )
            tx_hash = self.substrate_tx.submit_extrinsic(
                extrinsic=extrinsic,
                wait_for_inclusion=self.wait_for_inclusion,
                wait_for_finalization=self.wait_for_finalization
            )
            if isinstance(tx_hash, str):
                self.logger.info("Fast submit ok: %s.%s hash=%s", call_module, func_used, tx_hash)
                return tx_hash
            try:
                h = tx_hash.extrinsic_hash  # type: ignore[attr-defined]
                self.logger.info("Fast submit ok: %s.%s hash=%s", call_module, func_used, h)
                return h
            except Exception:
                self.logger.info("Fast submit ok (no hash str)")
                return "ok"
        except Exception as e:
            es = str(e)
            self.logger.warning("Fast submit error: %s", es)
            if any(x in es for x in ("Priority is too low", "Future", "Stale", "Old")):
                nm = self._nonce_mgr_for(signer_keypair)
                if nm:
                    nm.invalidate()
                    nm.refresh()
            return None

    def _submit_extrinsic_multi(self, module_candidates: List[str], func_candidates: List[str], params: Dict[str, Any], signer_keypair) -> Optional[str]:
        """Try multiple module and function name variants to compose and submit an extrinsic quickly."""
        for mod in module_candidates:
            try:
                res = self._submit_extrinsic_fast(mod, func_candidates, params, signer_keypair)
                if res:
                    return res
            except Exception:
                # _submit_extrinsic_fast already logs details and nonce handling
                continue
        self.logger.debug("No matching extrinsic among modules=%s funcs=%s", module_candidates, func_candidates)
        return None

    def submit_stake(self, netuid: Optional[int] = None) -> bool:
        """Submit stake extrinsic quickly with retries (prefer fast path)."""
        if netuid is None:
            netuid = self.subnet_id
        amount = bt.Balance.from_tao(self.stake_amount_tao)
        hot_ss58 = self.target_hotkey if self.target_hotkey else self.wallet.hotkey.ss58_address

        max_retries = int(self.cfg["max_retries"])
        backoff = float(self.cfg["retry_backoff_seconds"])
        max_backoff = float(self.cfg["max_backoff_seconds"])

        for attempt in range(1, max_retries + 1):
            try:
                if self.fast_mode:
                    self.logger.info("Submitting STAKE fast %.8f TAO on subnet %s (attempt %d/%d)", self.stake_amount_tao, str(netuid), attempt, max_retries)
                    modules = (list(dict.fromkeys([self.call_pallet, "SubtensorModule"]))
                               if self.call_pallet else ["SubtensorModule"])
                    funcs = self.stake_call_candidates
                    # Param variants across runtime versions
                    for pv in (
                        {"hotkey": hot_ss58, "netuid": int(netuid), "amount_staked": int(amount.rao)},
                        {"netuid": int(netuid), "amount_staked": int(amount.rao)},
                        {"hotkey": hot_ss58, "netuid": int(netuid), "amount": int(amount.rao)},
                        {"netuid": int(netuid), "amount": int(amount.rao)},
                        {"hotkey": hot_ss58, "netuid": int(netuid), "stake_to_be_added": int(amount.rao)},
                        {"netuid": int(netuid), "stake_to_be_added": int(amount.rao)}
                    ):
                        self.logger.debug("Trying fast compose %s.%s with params=%s", (modules[0] if modules else "SubtensorModule"), (funcs[0] if funcs else "add_stake"), list(pv.keys()))
                        if self._submit_extrinsic_multi(modules, funcs, pv, self.wallet.coldkey):
                            return True
                    self.logger.debug("Fast path failed; falling back to SDK path.")
                    if self.no_sdk_fallback:
                        self.logger.warning("no_sdk_fallback=true: skipping SDK stake path; will retry fast path only")
                        time.sleep(min(backoff, max_backoff))
                        backoff = min(backoff * 2, max_backoff)
                        continue

                # SDK fallback (only if allowed)
                if not self.no_sdk_fallback:
                    self.logger.info("Submitting STAKE via SDK %.8f TAO on subnet %d (attempt %d/%d)", self.stake_amount_tao, netuid, attempt, max_retries)
                    success = self.subtensor.add_stake(
                        wallet=self.wallet,
                        hotkey_ss58=hot_ss58,
                        amount=amount,
                        netuid=netuid,
                        wait_for_inclusion=self.wait_for_inclusion,
                        wait_for_finalization=self.wait_for_finalization
                    )
                    if success:
                        self.logger.info("Stake extrinsic submitted successfully (SDK)")
                        return True
                    else:
                        self.logger.warning("Stake extrinsic unsuccessful (SDK) (attempt %d/%d)", attempt, max_retries)
            except Exception as e:
                self.logger.warning("Stake submit error: %s", str(e))
                if self.nonce_mgr:
                    self.nonce_mgr.invalidate()

            if attempt < max_retries:
                time.sleep(min(backoff, max_backoff))
                backoff = min(backoff * 2, max_backoff)

        self.logger.error("Failed to submit stake after %d attempts", max_retries)
        return False

    def submit_unstake(self, netuid: Optional[int] = None) -> bool:
        """Submit unstake extrinsic quickly with retries (prefer fast path)."""
        if netuid is None:
            netuid = self.subnet_id
        
        self.logger.info("Unstaking full amount from subnet %d", netuid)
        
        hot_ss58 = self.target_hotkey if self.target_hotkey else self.wallet.hotkey.ss58_address

        max_retries = int(self.cfg["max_retries"])
        backoff = float(self.cfg["retry_backoff_seconds"])
        max_backoff = float(self.cfg["max_backoff_seconds"])

        for attempt in range(1, max_retries + 1):
            try:
                if self.fast_mode:
                    self.logger.info("Submitting UNSTAKE (full) fast on subnet %s (attempt %d/%d)", str(netuid), attempt, max_retries)
                    modules = (list(dict.fromkeys([self.call_pallet, "SubtensorModule"]))
                               if self.call_pallet else ["SubtensorModule"])
                    funcs = self.unstake_call_candidates
                    # Try both signers if configured to auto, otherwise the selected one
                    signers_to_try = []
                    us = self.unstake_signer
                    if us == "hotkey":
                        signers_to_try = [self.wallet.hotkey]
                    elif us == "coldkey":
                        signers_to_try = [self.wallet.coldkey]
                    else:
                        signers_to_try = [self.wallet.coldkey, self.wallet.hotkey]
                    # Try remove_stake_full_limit first (requires limit_price param)
                    for signer in signers_to_try:
                        # Try full unstake with limit_price (prevents price slippage)
                        # limit_price of 0 means no price limit (unstake at any price)
                        for pv in (
                            {"hotkey": hot_ss58, "netuid": int(netuid), "limit_price": 0},
                            {"hotkey": hot_ss58, "netuid": int(netuid), "limit_price": "0"},
                            {"netuid": int(netuid), "limit_price": 0},
                            {"netuid": int(netuid), "limit_price": "0"}
                        ):
                            self.logger.debug(
                                "Trying fast compose remove_stake_full_limit with params=%s (signer=%s)",
                                list(pv.keys()),
                                ("hotkey" if signer is self.wallet.hotkey else "coldkey")
                            )
                            if self._submit_extrinsic_multi(modules, ["remove_stake_full_limit"], pv, signer):
                                return True
                        
                        # Fallback to remove_stake with amount if full unstake not available
                        amount = bt.Balance.from_tao(self.stake_amount_tao)
                        amount_rao = int(amount.rao)
                        for pv in (
                            {"hotkey": hot_ss58, "netuid": int(netuid), "amount_unstaked": amount_rao},
                            {"hotkey": hot_ss58, "netuid": int(netuid), "alpha_unstaked": amount_rao},
                            {"hotkey": hot_ss58, "netuid": int(netuid), "amount": amount_rao},
                            {"netuid": int(netuid), "amount_unstaked": amount_rao},
                            {"netuid": int(netuid), "alpha_unstaked": amount_rao},
                            {"netuid": int(netuid), "amount": amount_rao}
                        ):
                            self.logger.debug(
                                "Trying fast compose remove_stake with params=%s (signer=%s)",
                                list(pv.keys()),
                                ("hotkey" if signer is self.wallet.hotkey else "coldkey")
                            )
                            if self._submit_extrinsic_multi(modules, ["remove_stake"], pv, signer):
                                return True
                    self.logger.debug("Fast path failed; falling back to SDK path.")
                    if self.no_sdk_fallback:
                        self.logger.warning("no_sdk_fallback=true: skipping SDK unstake path; will retry fast path only")
                        time.sleep(min(backoff, max_backoff))
                        backoff = min(backoff * 2, max_backoff)
                        continue

                # SDK fallback (only if allowed)
                if not self.no_sdk_fallback:
                    amount_bal = bt.Balance.from_rao(amount_rao)
                    self.logger.info("Submitting UNSTAKE via SDK %d rao on subnet %d (attempt %d/%d)", amount_rao, netuid, attempt, max_retries)
                    success = self.subtensor.unstake(
                        wallet=self.wallet,
                        hotkey_ss58=hot_ss58,
                        amount=amount_bal,
                        netuid=netuid,
                        wait_for_inclusion=self.wait_for_inclusion,
                        wait_for_finalization=self.wait_for_finalization
                    )
                    if success:
                        self.logger.info("Unstake extrinsic submitted successfully (SDK)")
                        return True
                    else:
                        self.logger.warning("Unstake extrinsic unsuccessful (SDK) (attempt %d/%d)", attempt, max_retries)
            except Exception as e:
                self.logger.warning("Unstake submit error: %s", str(e))
                if self.nonce_mgr:
                    self.nonce_mgr.invalidate()

            if attempt < max_retries:
                time.sleep(min(backoff, max_backoff))
                backoff = min(backoff * 2, max_backoff)

        self.logger.error("Failed to submit unstake after %d attempts", max_retries)
        return False

    def analyze_block(self, block_number: int):
        """Fetch events for block_number, count stakes per subnet, trigger stake if any count >= 2."""
        try:
            # RATE LIMIT PROTECTION: Check if enough time has passed since last operation
            time_since_last_op = time.time() - self.last_operation_time
            if self.last_operation_time > 0 and time_since_last_op < 30:
                remaining = 30 - time_since_last_op
                self.logger.debug("Skipping block %d analysis: %.1f seconds remaining in cooldown period", 
                                block_number, remaining)
                return
            
            events = self._events_for_block(block_number)
            # Export raw events to JSON per block if enabled
            if self.export_events:
                self._export_block_events(block_number, events)

            # Count StakeAdded (trigger) and StakeRemoved (diagnostics)
            counts_added = self._count_stake_added_by_subnet(events)
            counts_removed = self._count_stake_removed_by_subnet(events)
            if counts_removed:
                self.logger.debug("Block %d: StakeRemoved per subnet = %s", block_number, counts_removed)

            # Capture our last stake alpha for use in next-block unstake

            # Count StakeAdded (trigger) and StakeRemoved (diagnostics)
            counts_added = self._count_stake_added_by_subnet(events)
            counts_removed = self._count_stake_removed_by_subnet(events)
            if counts_removed:
                self.logger.debug("Block %d: StakeRemoved per subnet = %s", block_number, counts_removed)

            # Capture our last stake alpha for use in next-block unstake
            self._update_last_alpha_from_events(events)

            if not counts_added:
                self.logger.debug("Block %d: no StakeAdded events", block_number)
                return

            # Global gate: if any unstake is pending, skip all new stakes until it executes
            if self.block_new_stakes_globally and len(self.pending_unstake_blocks) > 0:
                self.logger.info("Pending unstake(s) present %s; skipping new stake triggers until unstake executes.",
                                 dict(self.pending_unstake_blocks))
                return

            # Determine which subnets to consider based on mode
            candidate_netuids: List[int] = []
            if self.subnet_mode == "any":
                # Filter out subnet 0 as per requirements
                candidate_netuids = [n for n, c in counts_added.items() if c >= 2 and n != 0]
            else:
                # Skip subnet 0 even in single mode
                if self.subnet_id != 0 and counts_added.get(self.subnet_id, 0) >= 2:
                    candidate_netuids = [self.subnet_id]
            
            # RATE LIMIT PROTECTION: Limit to only 1 subnet per block
            if candidate_netuids:
                if len(candidate_netuids) > 1:
                    self.logger.info("Multiple subnets triggered (%s). Limiting to 1 subnet per block to avoid rate limits.", candidate_netuids)
                    self.logger.info("Processing subnet %d only. Remaining subnets %s will be processed in future blocks if triggers persist.", 
                                   candidate_netuids[0], candidate_netuids[1:])
                # Only process the first subnet
                candidate_netuids = [candidate_netuids[0]]

            for netuid in candidate_netuids:
                if not self.cfg.get("allow_overlap_triggers", False) and netuid in self.pending_unstake_blocks:
                    self.logger.debug("Unstake pending for netuid %d; skip trigger at block %d", netuid, block_number)
                    continue

                # Ensure we don't double-trigger on same block for same subnet
                if self.last_staked_block_map.get(netuid) == block_number:
                    self.logger.debug("Already staked on block %d for subnet %d; skipping", block_number, netuid)
                    continue

                self.logger.info("Trigger condition met at block %d: >=2 StakeAdded on subnet %d (count=%d)",
                                 block_number, netuid, counts_added.get(netuid, 0))
                self.trigger_stake(block_number, netuid)
        except Exception as e:
            self.logger.warning("analyze_block error for block %d: %s", block_number, str(e))

    def trigger_stake(self, block_number: int, netuid: Optional[int] = None):
        """Submit stake and record next block for unstake on the given subnet."""
        if netuid is None:
            netuid = self.subnet_id
        ok = self.submit_stake(netuid)
        if ok:
            self.last_staked_block_map[netuid] = block_number
            self.pending_unstake_blocks[netuid] = block_number + 1
            # update legacy for back-compat if single mode
            if self.subnet_mode == "single":
                self._legacy_last_staked_block = block_number
                self._legacy_pending_unstake_block = block_number + 1
            self._save_state()
            self.logger.info("Scheduled UNSTAKE at block %d for subnet %d", block_number + 1, netuid)

    def execute_unstake(self, current_block: int):
        """Submit unstake for any subnet whose scheduled block <= current_block."""
        due: List[int] = []
        for netuid, when in list(self.pending_unstake_blocks.items()):
            if current_block >= when:
                due.append(netuid)

        for netuid in due:
            self.logger.info("Unstake due at block %d for subnet %d (scheduled: %d)", current_block, int(netuid), int(self.pending_unstake_blocks.get(netuid, -1)))
            ok = self.submit_unstake(netuid)
            if ok:
                del self.pending_unstake_blocks[netuid]
                if self.subnet_mode == "single":
                    self._legacy_pending_unstake_block = None
                self._save_state()
                
                # RATE LIMIT PROTECTION: Wait 30 seconds after successful unstake
                self.logger.info("Waiting 30 seconds after successful unstake to avoid rate limiting...")
                time.sleep(30)
                
                # Update last operation time
                self.last_operation_time = time.time()
            else:
                self.logger.warning("Unstake submission failed for subnet %d; will retry on next block", netuid)

    def _header_subscription_loop(self, handler, q: "queue.Queue[Any]", sub_exc: Dict[str, Any]):
        """Background thread function: subscribe_block_headers(handler)."""
        try:
            self.substrate.subscribe_block_headers(handler)
        except Exception as e:
            sub_exc["err"] = e
            try:
                q.put_nowait({"__error__": str(e)})
            except Exception:
                pass

    def _parse_block_number(self, header_obj: Any) -> Optional[int]:
        try:
            number = header_obj["header"]["number"]
            return int(number, 16) if isinstance(number, str) else int(number)
        except Exception:
            try:
                number = header_obj.get("number", header_obj.get("block", {}).get("header", {}).get("number"))
                return int(number, 16) if isinstance(number, str) else int(number)
            except Exception:
                return None

    def monitor_blocks(self):
        """Continuous monitoring: callback (preferred) or fallback to iterator/poll, with reconnects."""
        mode_info = f"{self.subnet_mode} mode"
        self.logger.info("Starting block monitor (%s). Stake amount: %.8f TAO | fast_mode=%s",
                         mode_info, self.stake_amount_tao, self.fast_mode)

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        backoff = float(self.cfg["ws_reconnect_backoff_seconds"])
        max_backoff = float(self.cfg["ws_reconnect_max_backoff_seconds"])

        while not self._shutdown:
            try:
                # Decide callback vs poll
                use_callback = False
                if self.subscription_mode == "poll":
                    use_callback = False
                elif self.subscription_mode == "callback":
                    use_callback = True
                else:
                    try:
                        sig = inspect.signature(self.substrate.subscribe_block_headers)
                        required = [p for p in sig.parameters.values() if p.default is p.empty]
                        use_callback = len(required) >= 1
                    except Exception:
                        use_callback = False

                if use_callback:
                    self.logger.info("Subscribing to new block headers (callback mode)...")
                    q: "queue.Queue[Any]" = queue.Queue(maxsize=1024)
                    sub_exc: Dict[str, Any] = {"err": None}

                    def handler(obj, update_nr=None, subscription_id=None):
                        try:
                            q.put_nowait(obj)
                        except Exception:
                            pass

                    t = threading.Thread(target=self._header_subscription_loop, args=(handler, q, sub_exc), daemon=True)
                    t.start()
                    self.logger.info("Subscription thread started (callback mode).")

                    received_any_header = False
                    deadline = time.time() + self.header_timeout

                    while not self._shutdown:
                        # Thread liveness
                        if not t.is_alive():
                            if sub_exc["err"] is not None:
                                raise sub_exc["err"]
                            raise RuntimeError("Header subscription thread stopped")
                        try:
                            item = q.get(timeout=1.0)
                            if isinstance(item, dict) and "__error__" in item:
                                raise RuntimeError(f"Subscription error: {item['__error__']}")
                        except queue.Empty:
                            if not received_any_header and time.time() > deadline:
                                self.logger.info("No headers received within %.1fs. Falling back to polling mode.", self.header_timeout)
                                break
                            continue

                        block_number = self._parse_block_number(item)
                        if block_number is None:
                            continue
                        received_any_header = True
                        self.last_analyzed_block = block_number
                        self.logger.debug("New block: %d", block_number)
                        self.execute_unstake(block_number)
                        self.analyze_block(block_number)

                else:
                    # Always poll when callback mode is unavailable on this substrate-interface version
                    self.logger.info("Polling current block (subscription iterator unsupported on this runtime)...")
                    last_seen: Optional[int] = None
                    while not self._shutdown:
                        try:
                            current = self.subtensor.get_current_block()
                            if last_seen is None or current > last_seen:
                                last_seen = current
                                self.last_analyzed_block = current
                                self.logger.debug("New block (poll): %d", current)
                                self.execute_unstake(current)
                                self.analyze_block(current)
                            time.sleep(self.poll_interval)
                        except Exception as pe:
                            self.logger.warning("Polling error: %s", str(pe))
                            break

                if not self._shutdown:
                    time.sleep(0.05)

            except Exception as e:
                if self._shutdown:
                    break
                self.logger.warning("Subscription error: %s", str(e))
                self.logger.info("Reconnecting in %.2fs...", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                try:
                    # Recreate primary substrate client
                    self.subtensor = self._new_subtensor()
                    self.substrate = self.subtensor.substrate
                except Exception:
                    pass

        self.logger.info("Monitor stopped (shutdown).")

    def _handle_signal(self, signum, frame):
        self.logger.info("Signal %s received; shutting down...", signum)
        self._shutdown = True


# Module-level functions for compatibility
def monitor_blocks():
    if BOT is None:
        raise RuntimeError("Bot not initialized")
    BOT.monitor_blocks()


def analyze_block(block_number: int):
    if BOT is None:
        raise RuntimeError("Bot not initialized")
    BOT.analyze_block(block_number)


def trigger_stake(block_number: int):
    if BOT is None:
        raise RuntimeError("Bot not initialized")
    BOT.trigger_stake(block_number)


def execute_unstake(current_block: int):
    if BOT is None:
        raise RuntimeError("Bot not initialized")
    BOT.execute_unstake(current_block)


def submit_stake():
    if BOT is None:
        raise RuntimeError("Bot not initialized")
    return BOT.submit_stake()


def submit_unstake():
    if BOT is None:
        raise RuntimeError("Bot not initialized")
    return BOT.submit_unstake()


def load_or_create_config(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        if 'YAML_AVAILABLE' in globals() and YAML_AVAILABLE:
            with open(path, "w") as f:
                yaml.safe_dump(DEFAULT_CONFIG, f, sort_keys=False)
        else:
            _simple_dump_yaml(path, DEFAULT_CONFIG)
        print(f"Created default config at {path}. Edit it and re-run if needed.")
        return DEFAULT_CONFIG.copy()

    if 'YAML_AVAILABLE' in globals() and YAML_AVAILABLE:
        with open(path, "r") as f:
            cfg = yaml.safe_load(f) or {}
    else:
        cfg = _simple_parse_yaml(path)

    merged = DEFAULT_CONFIG.copy()
    merged.update(cfg)
    return merged


def main():
    cfg = load_or_create_config(CONFIG_PATH)

    print("Loaded config:", json.dumps(cfg, indent=2))

    global BOT
    BOT = AutoStakeBot(cfg)

    try:
        BOT.initialize()
    except Exception as e:
        print(f"Initialization failed: {e}")
        sys.exit(1)

    BOT.monitor_blocks()


if __name__ == "__main__":
    main()
