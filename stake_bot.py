#!/usr/bin/env python3
"""
Bittensor Automated Event-Driven Staking Bot (Production-optimized)

Objective:
- Monitor every new block via websocket
- For each block, count StakeAdded events on the target subnet (default: 63)
- If count >= 2 in block N -> immediately submit stake extrinsic (best-effort same block, realistically N+1)
- On block N+1 -> automatically submit unstake extrinsic
- Repeat whenever the trigger condition is met

Run:
    python stake_bot.py

Config:
    Edit config.yaml (auto-created on first run) to set:
      - wallet_name, hotkey_name
      - stake_amount (TAO)
      - subnet_id (default 63)
      - network ("finney" mainnet or "test")
      - logging options
      - retry/backoff options
      - fast_mode, tip_tao, persist_state, state_file

Technical Notes:
- Uses Bittensor SDK for wallet and extrinsics
- Uses Substrate websocket subscription for low-latency block monitoring
- Nonce/priority/timeout errors are retried with backoff
- Zero-wait submits by default (no inclusion/finalization waits)
- Optional fast path: direct extrinsic composition with explicit nonce & tip
- Persists state to survive restarts (pending unstake)
"""

import os
import sys
import time
import json
import signal
import logging
from logging.handlers import RotatingFileHandler
from typing import Optional, Dict, Any, List
import inspect
import queue
import threading

import bittensor as bt
try:
    from bittensor.wallet import Wallet as WalletCls  # newer SDK path
except Exception:
    WalletCls = None

# Optional YAML dependency with minimal fallback parser
try:
    import yaml  # PyYAML
    YAML_AVAILABLE = True
except Exception:
    YAML_AVAILABLE = False


DEFAULT_CONFIG = {
    "network": "finney",                # "finney" (mainnet) or "test"
    "wallet_name": "default",
    "hotkey_name": "default",
    "wallet_password": "",
    "target_hotkey": "",                # optional: stake to this hotkey (validator) if set; otherwise use your wallet's hotkey
    "stake_amount": 0.05,               # TAO
    "subnet_id": 63,                    # default target subnet

    # Performance & behavior
    "fast_mode": True,                  # Use explicit nonce & tip via substrate-interface when possible
    "tip_tao": 0.0,                     # optional tip to improve priority
    "wait_for_inclusion": False,        # defaults tuned for SPEED
    "wait_for_finalization": False,

    # Retries/backoff
    "max_retries": 5,
    "retry_backoff_seconds": 0.5,       # starting backoff
    "max_backoff_seconds": 4.0,

    # Websocket reconnects
    "ws_reconnect_backoff_seconds": 1.0,
    "ws_reconnect_max_backoff_seconds": 8.0,

    # Subscription strategy
    "subscription_mode": "auto",        # auto | callback | poll
    "header_wait_timeout_seconds": 15.0,

    # Logging
    "log_level": "INFO",                # DEBUG | INFO | WARNING | ERROR
    "log_to_file": True,
    "log_file": "stake_bot.log",

    # State persistence
    "persist_state": True,
    "state_file": "stake_state.json",

    # Trigger safety
    "allow_overlap_triggers": False     # if False: don't trigger a new stake while an unstake is pending
}

CONFIG_PATH = os.environ.get("STAKE_BOT_CONFIG", "config.yaml")


def _simple_parse_yaml(path: str) -> Dict[str, Any]:
    """
    Minimal YAML parser for simple 'key: value' pairs (booleans/numbers/strings).
    Used only when PyYAML is unavailable to avoid extra dependency.
    """
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
                # Strip end-of-line comments
                if " #" in val:
                    val = val.split(" #", 1)[0].strip()
                # Convert booleans
                low = val.lower()
                if low in ("true", "false"):
                    data[key] = (low == "true")
                    continue
                # Convert numbers
                try:
                    if "." in val:
                        data[key] = float(val)
                    else:
                        data[key] = int(val)
                    continue
                except Exception:
                    pass
                # Strip quotes
                if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                data[key] = val
    except Exception:
        return {}
    return data


def _simple_dump_yaml(path: str, obj: Dict[str, Any]) -> None:
    """Write a minimal YAML file with simple key: value pairs."""
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


# Global bot instance is created in main() so module-level functions can delegate to it.
BOT = None


class AutoStakeBot:
    def __init__(self, config: Dict[str, Any]):
        self.cfg = config
        self._setup_logging()
        self.logger = logging.getLogger("stake_bot")

        self.network = self.cfg["network"]
        self.wallet_name = self.cfg["wallet_name"]
        self.hotkey_name = self.cfg["hotkey_name"]
        self.target_hotkey = str(self.cfg.get("target_hotkey", "")).strip()
        self.subnet_id = int(self.cfg["subnet_id"])
        self.stake_amount_tao = float(self.cfg["stake_amount"])

        self.fast_mode = bool(self.cfg.get("fast_mode", True))
        self.tip_tao = float(self.cfg.get("tip_tao", 0.0))
        self.wait_for_inclusion = bool(self.cfg.get("wait_for_inclusion", False))
        self.wait_for_finalization = bool(self.cfg.get("wait_for_finalization", False))

        self.subtensor: Optional[bt.Subtensor] = None
        self.substrate = None
        # Dedicated client for event queries to avoid websocket recv conflicts with subscription thread
        self.events_subtensor: Optional[bt.Subtensor] = None
        self.substrate_events = None
        self.wallet: Optional[Any] = None
        self.nonce_mgr: Optional[NonceManager] = None

        # State for trigger/unstake coordination
        self.last_analyzed_block: Optional[int] = None
        self.last_staked_block: Optional[int] = None
        self.pending_unstake_block: Optional[int] = None
        self._shutdown = False

        # persistence
        self.persist_state = bool(self.cfg.get("persist_state", True))
        self.state_file = str(self.cfg.get("state_file", "stake_state.json"))

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

    def _create_wallet(self):
        """
        Create a Bittensor wallet object compatible across SDK versions.
        Tries:
          1) WalletCls from bittensor.wallet (newer SDKs)
          2) bt.wallet(name=..., hotkey=...) (older SDKs)
        """
        # Newer SDK
        if 'WalletCls' in globals() and WalletCls is not None:
            try:
                return WalletCls(name=self.wallet_name, hotkey=self.hotkey_name)
            except Exception as e:
                self.logger.debug("WalletCls init failed: %s", e)

        # Older SDK: function-style factory on bittensor module
        try:
            if hasattr(bt, "wallet") and callable(getattr(bt, "wallet")):
                return bt.wallet(name=self.wallet_name, hotkey=self.hotkey_name)
        except Exception as e:
            self.logger.debug("bt.wallet init failed: %s", e)

        raise RuntimeError("Bittensor Wallet API not found. Install compatible bittensor in this venv, e.g.: pip install 'bittensor==9.15.3'")

    def _load_state(self):
        if not self.persist_state:
            return
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, "r") as f:
                    s = json.load(f)
                self.last_staked_block = s.get("last_staked_block")
                self.pending_unstake_block = s.get("pending_unstake_block")
                self.logger.info("Restored state: last_staked_block=%s pending_unstake_block=%s",
                                 self.last_staked_block, self.pending_unstake_block)
        except Exception as e:
            self.logger.warning("Failed to load state: %s", str(e))

    def _save_state(self):
        if not self.persist_state:
            return
        try:
            s = {
                "last_staked_block": self.last_staked_block,
                "pending_unstake_block": self.pending_unstake_block
            }
            with open(self.state_file, "w") as f:
                json.dump(s, f)
        except Exception as e:
            self.logger.warning("Failed to save state: %s", str(e))

    def initialize(self):
        self.logger.info("Initializing wallet and network connection...")
        # Wallet (SDK-compatible creation)
        self.wallet = self._create_wallet()
        self.wallet.create_if_non_existent()

        # Unlock coldkey once per process (supports config/env), prompt only once if needed
        try:
            pw = str(self.cfg.get("wallet_password", "")).strip()
            if pw:
                os.environ["BT_WALLET_PASSWORD"] = pw
            try:
                self.wallet.unlock_coldkey()
                self.logger.info("Wallet unlocked")
            except Exception as e1:
                import getpass
                self.logger.info("Wallet unlock needs password; prompting once...")
                pw_input = getpass.getpass("Wallet password: ")
                if pw_input:
                    os.environ["BT_WALLET_PASSWORD"] = pw_input
                self.wallet.unlock_coldkey()
                self.logger.info("Wallet unlocked")
        except Exception as e:
            # Wallet may be unencrypted or already unlocked
            self.logger.info("Wallet loaded (unencrypted or already unlocked): %s", str(e))

        # Subtensor / network connect
        self.subtensor = bt.Subtensor(network=self.network)
        # Underlying substrate client (websocket)
        self.substrate = self.subtensor.substrate
        # Separate substrate for event queries to avoid 'recv already running' conflicts
        try:
            self.events_subtensor = bt.Subtensor(network=self.network)
            self.substrate_events = self.events_subtensor.substrate
        except Exception as e:
            self.logger.warning("Failed to initialize separate events substrate: %s", str(e))
            self.substrate_events = self.substrate
        self.logger.info("Connected to network: %s", self.network)

        # Nonce manager for the signer (coldkey is the payer)
        signer_ss58 = self.wallet.coldkeypub.ss58_address
        self.nonce_mgr = NonceManager(self.substrate, signer_ss58, self.logger)

        # Basic wallet info
        cold_ss58 = self.wallet.coldkeypub.ss58_address
        hot_ss58 = self.wallet.hotkey.ss58_address
        bal = self.subtensor.get_balance(cold_ss58)
        self.logger.info("Coldkey: %s | Hotkey: %s | Balance: %s TAO", cold_ss58, hot_ss58, bal.tao)

        if bal.tao < self.stake_amount_tao:
            raise RuntimeError(f"Insufficient balance {bal.tao} < required {self.stake_amount_tao} TAO")

        # Restore persisted state if any
        self._load_state()

    def _ensure_events_client(self):
        """Ensure a dedicated events substrate client exists (separate websocket)."""
        if self.substrate_events is None:
            try:
                self.events_subtensor = bt.Subtensor(network=self.network)
                self.substrate_events = self.events_subtensor.substrate
            except Exception as e:
                self.logger.warning("Recreating events substrate failed: %s", str(e))
                self.substrate_events = self.substrate

    def _events_for_block(self, block_number: int):
        """Fetch events for a given block number. Returns list of event records, or []"""
        # Always use the dedicated events substrate to avoid recv conflict with subscription thread
        self._ensure_events_client()
        try:
            block_hash = self.substrate_events.get_block_hash(block_number)
            if block_hash is None:
                return []
            events = self.substrate_events.get_events(block_hash=block_hash)
            return events or []
        except Exception as e:
            es = str(e)
            # Handle recv conflicts by recreating the events substrate and retrying once
            if "cannot call recv while another thread is already running recv" in es or "recv_streaming" in es:
                self.logger.warning("Events client recv conflict; recreating events substrate and retrying...")
                try:
                    # Recreate client
                    self.events_subtensor = bt.Subtensor(network=self.network)
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

    def _count_stake_added_for_subnet(self, events, target_subnet: int) -> int:
        """Count events that look like StakeAdded on target subnet."""
        count = 0

        # Known variations of event naming
        accepted_event_ids = {
            "StakeAdded", "StakeIncrease", "StakeIncreased", "AddStake"
        }
        accepted_modules = {"SubtensorModule", "Subtensor"}  # pallet naming variants

        for ev in events:
            try:
                # substrate-interface yields EventRecord with .value dict
                evval = ev.value if hasattr(ev, "value") else ev
                evt = evval.get("event", {})
                module_id = evt.get("module_id") or evt.get("pallet") or ""
                event_id = evt.get("event_id") or evt.get("event") or ""

                if module_id not in accepted_modules or event_id not in accepted_event_ids:
                    continue

                # params may be under "attributes" or "params"
                params = evt.get("attributes") or evt.get("params") or []
                # Normalize to list of dicts with name/value
                parsed_params = []
                for p in params:
                    if isinstance(p, dict):
                        parsed_params.append(p)
                    else:
                        parsed_params.append({"name": "", "value": p})

                ev_netuid = None
                for p in parsed_params:
                    name = (p.get("name") or "").lower()
                    if name == "netuid":
                        try:
                            ev_netuid = int(p.get("value"))
                        except Exception:
                            pass
                        break
                    if ev_netuid is None and isinstance(p.get("value"), int):
                        ev_netuid = int(p.get("value"))

                if ev_netuid is None:
                    continue

                if ev_netuid == target_subnet:
                    count += 1
            except Exception as e:
                self.logger.debug("Error parsing event: %s", str(e))
                continue

        return count

    def _best_effort_tip_rao(self) -> Optional[int]:
        if self.tip_tao <= 0:
            return None
        try:
            return int(bt.Balance.from_tao(self.tip_tao).rao)
        except Exception:
            return None

    def _resolve_call_name(self, pallet: str, candidates: List[str]) -> Optional[str]:
        for name in candidates:
            try:
                # probe by attempting to load metadata for the function
                _ = self.substrate.get_metadata_call_function(pallet, name)
                return name
            except Exception:
                continue
        return None

    def _submit_extrinsic_fast(self, call_module: str, call_function_candidates: List[str], params: Dict[str, Any]) -> Optional[str]:
        """
        Compose and submit extrinsic with explicit nonce & tip, without waiting.
        Returns tx hash on (apparent) success or None on failure.
        """
        try:
            func_used = None
            call = None
            for func in call_function_candidates:
                try:
                    call = self.substrate.compose_call(
                        call_module=call_module,
                        call_function=func,
                        call_params=params
                    )
                    func_used = func
                    break
                except Exception:
                    continue

            if call is None:
                self.logger.debug("Fast mode: no matching function in %s among %s", call_module, call_function_candidates)
                return None

            # Explicit nonce & tip
            nonce = self.nonce_mgr.next_and_increment() if self.nonce_mgr else None
            tip_rao = self._best_effort_tip_rao()

            extrinsic = self.substrate.create_signed_extrinsic(
                call=call,
                keypair=self.wallet.coldkey,
                tip=tip_rao if tip_rao is not None else 0,
                nonce=nonce
            )

            tx_hash = self.substrate.submit_extrinsic(
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
            if "Priority is too low" in es or "Future" in es or "Stale" in es or "Old" in es:
                if self.nonce_mgr:
                    self.nonce_mgr.invalidate()
                    self.nonce_mgr.refresh()
            return None

    def submit_stake(self) -> bool:
        """Submit stake extrinsic quickly with retries, prefer fast mode."""
        amount = bt.Balance.from_tao(self.stake_amount_tao)
        hot_ss58 = self.target_hotkey if getattr(self, "target_hotkey", "") else self.wallet.hotkey.ss58_address
        netuid = self.subnet_id

        max_retries = int(self.cfg["max_retries"])
        backoff = float(self.cfg["retry_backoff_seconds"])
        max_backoff = float(self.cfg["max_backoff_seconds"])

        for attempt in range(1, max_retries + 1):
            try:
                if self.fast_mode:
                    self.logger.info("Submitting STAKE fast %.8f TAO on subnet %d (attempt %d/%d)", self.stake_amount_tao, netuid, attempt, max_retries)
                    params = {
                        "hotkey": hot_ss58,
                        "amount": int(amount.rao),
                        "netuid": int(netuid),
                    }
                    txh = self._submit_extrinsic_fast(
                        call_module="SubtensorModule",
                        call_function_candidates=["add_stake", "addStake"],
                        params=params
                    )
                    if txh:
                        return True
                    self.logger.debug("Fast path failed; falling back to Subtensor.add_stake()")

                # Fallback to SDK path
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
                    self.logger.warning("Stake extrinsic returned unsuccessful (SDK) (attempt %d/%d)", attempt, max_retries)
            except Exception as e:
                es = str(e)
                self.logger.warning("Stake submit error: %s", es)
                if self.nonce_mgr:
                    self.nonce_mgr.invalidate()

            if attempt < max_retries:
                time.sleep(min(backoff, max_backoff))
                backoff = min(backoff * 2, max_backoff)

        self.logger.error("Failed to submit stake after %d attempts", max_retries)
        return False

    def submit_unstake(self) -> bool:
        """Submit unstake extrinsic quickly with retries, prefer fast mode."""
        amount = bt.Balance.from_tao(self.stake_amount_tao)
        hot_ss58 = self.target_hotkey if getattr(self, "target_hotkey", "") else self.wallet.hotkey.ss58_address
        netuid = self.subnet_id

        max_retries = int(self.cfg["max_retries"])
        backoff = float(self.cfg["retry_backoff_seconds"])
        max_backoff = float(self.cfg["max_backoff_seconds"])

        for attempt in range(1, max_retries + 1):
            try:
                if self.fast_mode:
                    self.logger.info("Submitting UNSTAKE fast %.8f TAO on subnet %d (attempt %d/%d)", self.stake_amount_tao, netuid, attempt, max_retries)
                    params = {
                        "hotkey": hot_ss58,
                        "amount": int(amount.rao),
                        "netuid": int(netuid),
                    }
                    txh = self._submit_extrinsic_fast(
                        call_module="SubtensorModule",
                        call_function_candidates=["remove_stake", "removeStake"],
                        params=params
                    )
                    if txh:
                        return True
                    self.logger.debug("Fast path failed; falling back to Subtensor.unstake()")

                # Fallback to SDK path
                self.logger.info("Submitting UNSTAKE via SDK %.8f TAO on subnet %d (attempt %d/%d)", self.stake_amount_tao, netuid, attempt, max_retries)
                success = self.subtensor.unstake(
                    wallet=self.wallet,
                    hotkey_ss58=hot_ss58,
                    amount=amount,
                    netuid=netuid,
                    wait_for_inclusion=self.wait_for_inclusion,
                    wait_for_finalization=self.wait_for_finalization
                )
                if success:
                    self.logger.info("Unstake extrinsic submitted successfully (SDK)")
                    return True
                else:
                    self.logger.warning("Unstake extrinsic returned unsuccessful (SDK) (attempt %d/%d)", attempt, max_retries)
            except Exception as e:
                es = str(e)
                self.logger.warning("Unstake submit error: %s", es)
                if self.nonce_mgr:
                    self.nonce_mgr.invalidate()

            if attempt < max_retries:
                time.sleep(min(backoff, max_backoff))
                backoff = min(backoff * 2, max_backoff)

        self.logger.error("Failed to submit unstake after %d attempts", max_retries)
        return False

    def analyze_block(self, block_number: int):
        """Fetch events for block_number, count stakes for target subnet, trigger stake if count >= 2."""
        try:
            # Optional: prevent overlap triggers if unstake is pending
            if not self.cfg.get("allow_overlap_triggers", False) and self.pending_unstake_block is not None:
                # Avoid stacking positions before previous unstake executes
                self.logger.debug("Unstake pending for block %s; skip trigger at block %s",
                                  self.pending_unstake_block, block_number)
                return

            events = self._events_for_block(block_number)
            count = self._count_stake_added_for_subnet(events, self.subnet_id)
            self.logger.debug("Block %d: StakeAdded count on subnet %d = %d", block_number, self.subnet_id, count)

            if count >= 2:
                # Ensure we don't double-trigger on same block
                if self.last_staked_block == block_number:
                    self.logger.debug("Already staked on block %d; skipping", block_number)
                    return

                self.logger.info("Trigger condition met at block %d: >=2 StakeAdded on subnet %d", block_number, self.subnet_id)
                self.trigger_stake(block_number)
        except Exception as e:
            self.logger.warning("analyze_block error for block %d: %s", block_number, str(e))

    def trigger_stake(self, block_number: int):
        """Submit stake and record next block for unstake."""
        ok = self.submit_stake()
        if ok:
            self.last_staked_block = block_number
            self.pending_unstake_block = block_number + 1
            self._save_state()
            self.logger.info("Scheduled UNSTAKE at block %d", self.pending_unstake_block)

    def execute_unstake(self, current_block: int):
        """If pending_unstake_block == current_block (or already passed), submit unstake."""
        if self.pending_unstake_block is None:
            return

        # If we somehow missed the exact block, execute as soon as possible
        if current_block >= self.pending_unstake_block:
            self.logger.info("Unstake due at block %d (scheduled: %d)", current_block, self.pending_unstake_block)
            ok = self.submit_unstake()
            if ok:
                # Clear pending after successful submission
                self.pending_unstake_block = None
                self._save_state()
            else:
                # If unstake failed, keep pending and retry on next block
                self.logger.warning("Unstake submission failed; will retry on next block")

    def monitor_blocks(self):
        """Continuous monitoring of new blocks with subscription-callback if available, otherwise iterator or polling fallback."""
        self.logger.info("Starting block monitor. Target subnet: %d | Stake amount: %.8f TAO | fast_mode=%s",
                         self.subnet_id, self.stake_amount_tao, self.fast_mode)

        # Basic signal handlers
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        backoff = float(self.cfg["ws_reconnect_backoff_seconds"])
        max_backoff = float(self.cfg["ws_reconnect_max_backoff_seconds"])
        subscription_mode = str(self.cfg.get("subscription_mode", "auto")).lower()
        header_timeout = float(self.cfg.get("header_wait_timeout_seconds", 15.0))

        def parse_block_number(header_obj: Any) -> Optional[int]:
            try:
                number = header_obj["header"]["number"]
                return int(number, 16) if isinstance(number, str) else int(number)
            except Exception:
                try:
                    number = header_obj.get("number", header_obj.get("block", {}).get("header", {}).get("number"))
                    return int(number, 16) if isinstance(number, str) else int(number)
                except Exception:
                    return None

        while not self._shutdown:
            try:
                # Detect subscribe_block_headers signature and choose mode
                use_callback = False
                if subscription_mode == "poll":
                    use_callback = False
                elif subscription_mode == "callback":
                    use_callback = True
                else:
                    try:
                        sig = inspect.signature(self.substrate.subscribe_block_headers)
                        # If at least one required positional parameter is present, it's callback style
                        required = [p for p in sig.parameters.values() if p.default is p.empty]
                        use_callback = len(required) >= 1
                    except Exception:
                        use_callback = False

                if use_callback:
                    self.logger.info("Subscribing to new block headers (callback mode)...")
                    q: "queue.Queue[Any]" = queue.Queue(maxsize=1024)

                    def handler(obj, update_nr=None, subscription_id=None):
                        try:
                            q.put_nowait(obj)
                        except Exception:
                            # Drop if queue is full to avoid blocking
                            pass

                    # Start subscription in a separate context - this blocks, so we need to handle it differently
                    # The subscription call itself will block, so we consume from queue in the main thread
                    import threading
                    
                    def subscription_thread():
                        try:
                            self.substrate.subscribe_block_headers(handler)
                        except Exception as e:
                            self.logger.warning("Subscription thread error: %s", str(e))
                            q.put(None)  # Signal error
                    
                    thread = threading.Thread(target=subscription_thread, daemon=True)
                    thread.start()
                    self.logger.info("Subscription thread started (callback mode).")
                    received_any_header = False
                    deadline = time.time() + header_timeout

                    while not self._shutdown:
                        try:
                            header = q.get(timeout=5.0)
                            if header is None:
                                # Error signal from subscription thread
                                raise RuntimeError("Subscription thread encountered an error")
                        except queue.Empty:
                            # Check if thread is still alive
                            if not thread.is_alive():
                                raise RuntimeError("Subscription thread died")
                            # Fallback if no headers received within timeout
                            if not received_any_header and time.time() > deadline:
                                self.logger.info("No headers received within %.1fs. Falling back to polling mode.", header_timeout)
                                break
                            continue
                        
                        block_number = parse_block_number(header)
                        if block_number is None:
                            continue
                        received_any_header = True
                        self.last_analyzed_block = block_number
                        self.logger.debug("New block: %d", block_number)
                        # Unstake first, then analyze
                        self.execute_unstake(block_number)
                        self.analyze_block(block_number)
                else:
                    self.logger.info("Subscribing to new block headers (iterator mode or polling fallback)...")
                    # Try iterator mode first
                    try:
                        for header in self.substrate.subscribe_block_headers():
                            if self._shutdown:
                                break
                            block_number = parse_block_number(header)
                            if block_number is None:
                                continue
                            self.last_analyzed_block = block_number
                            self.logger.debug("New block: %d", block_number)
                            self.execute_unstake(block_number)
                            self.analyze_block(block_number)
                    except TypeError:
                        # Fallback to polling if iterator not supported in this SDK
                        self.logger.info("Iterator mode not supported; falling back to polling current block...")
                        last_seen = None
                        while not self._shutdown:
                            try:
                                current = self.subtensor.get_current_block()
                                if last_seen is None or current > last_seen:
                                    last_seen = current
                                    self.last_analyzed_block = current
                                    self.logger.debug("New block (poll): %d", current)
                                    self.execute_unstake(current)
                                    self.analyze_block(current)
                                time.sleep(0.5)
                            except Exception as pe:
                                self.logger.warning("Polling error: %s", str(pe))
                                break

                # Short breather before re-subscribing if loop exits normally
                if not self._shutdown:
                    time.sleep(0.05)

            except Exception as e:
                if self._shutdown:
                    break
                self.logger.warning("Subscription error: %s", str(e))
                self.logger.info("Reconnecting in %.2fs...", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                # Attempt to re-fetch substrate via subtensor (auto-reconnect)
                try:
                    self.substrate = self.subtensor.substrate
                except Exception:
                    pass

        self.logger.info("Monitor stopped (shutdown).")

    def _handle_signal(self, signum, frame):
        self.logger.info("Signal %s received; shutting down...", signum)
        self._shutdown = True


# Module-level functions expected by the spec, delegating to global BOT instance
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

    # Merge with defaults for missing keys
    merged = DEFAULT_CONFIG.copy()
    merged.update(cfg)
    return merged


def main():
    cfg = load_or_create_config(CONFIG_PATH)

    # Echo config (mask sensitive fields if any added later)
    to_log = {k: v for k, v in cfg.items()}
    print("Loaded config:", json.dumps(to_log, indent=2))

    global BOT
    BOT = AutoStakeBot(cfg)

    try:
        BOT.initialize()
    except Exception as e:
        print(f"Initialization failed: {e}")
        sys.exit(1)

    # Start monitoring loop (blocking)
    BOT.monitor_blocks()


if __name__ == "__main__":
    main()
