#!/usr/bin/env python3
"""
Bittensor Automated Event-Driven Staking Bot (WS-first, Production-optimized)

Objective:
- Monitor every new block with minimal latency
- Count StakeAdded events on the target subnet per block
- If count >= 2 for block N -> submit stake ASAP (best-effort N/N+1)
- Schedule unstake for N+1 and execute promptly
- Repeat reliably with robust reconnects, concurrency, and fallbacks

Key Architecture:
- Websocket-first:
  - Subscribe to new heads (best effort earliest visibility)
  - Optional: Subscribe to System.Events (storage) when available
- Separate websocket clients:
  - Client A: headers
  - Client B: events (optional)
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
    - stake_amount (TAO), subnet_id
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
from typing import Optional, Dict, Any, List, Tuple
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
    "subnet_id": 63,                     # default target subnet

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

    # Logging
    "log_level": "INFO",                 # DEBUG | INFO | WARNING | ERROR
    "log_to_file": True,
    "log_file": "stake_bot.log",

    # State persistence
    "persist_state": True,
    "state_file": "stake_state.json",

    # Trigger safety
    "allow_overlap_triggers": False      # if False: don't trigger stake while an unstake is pending
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
        self.target_hotkey = str(self.cfg.get("target_hotkey", "")).strip()
        self.subnet_id = int(self.cfg["subnet_id"])
        self.stake_amount_tao = float(self.cfg["stake_amount"])

        self.fast_mode = bool(self.cfg.get("fast_mode", True))
        self.tip_tao = float(self.cfg.get("tip_tao", 0.0))
        self.wait_for_inclusion = bool(self.cfg.get("wait_for_inclusion", False))
        self.wait_for_finalization = bool(self.cfg.get("wait_for_finalization", False))

        self.subscription_mode = str(self.cfg.get("subscription_mode", "auto")).lower()
        self.header_timeout = float(self.cfg.get("header_wait_timeout_seconds", 3.0))
        self.poll_interval = float(self.cfg.get("poll_interval_seconds", 0.5))

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

        # State (trigger/unstake coordination)
        self.last_analyzed_block: Optional[int] = None
        self.last_staked_block: Optional[int] = None
        self.pending_unstake_block: Optional[int] = None

        # Concurrency/threading
        self._shutdown = False

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
        try:
            if os.path.exists(self.cfg.get("state_file", "stake_state.json")):
                with open(self.cfg.get("state_file", "stake_state.json"), "r") as f:
                    s = json.load(f)
                self.last_staked_block = s.get("last_staked_block")
                self.pending_unstake_block = s.get("pending_unstake_block")
                self.logger.info("Restored state: last_staked_block=%s pending_unstake_block=%s",
                                 self.last_staked_block, self.pending_unstake_block)
        except Exception as e:
            self.logger.warning("Failed to load state: %s", str(e))

    def _save_state(self):
        if not self.cfg.get("persist_state", True):
            return
        try:
            s = {
                "last_staked_block": self.last_staked_block,
                "pending_unstake_block": self.pending_unstake_block
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
        # Compatible across SDK versions: prefer factory on bt
        wallet_factory = getattr(bt, "wallet", None)
        if callable(wallet_factory):
            self.wallet = wallet_factory(name=self.wallet_name, hotkey=self.hotkey_name)
        else:
            # Best-effort fallback
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

        self.logger.info("Connected to network: %s", self.network)

        # Nonce manager (signer is coldkey)
        signer_ss58 = self.wallet.coldkeypub.ss58_address
        self.nonce_mgr = NonceManager(self.substrate_tx, signer_ss58, self.logger)

        # Balance check
        bal = self.subtensor.get_balance(signer_ss58)
        self.logger.info("Coldkey: %s | Hotkey: %s | Balance: %s TAO",
                         signer_ss58, self.wallet.hotkey.ss58_address, bal.tao)
        if bal.tao < self.stake_amount_tao:
            raise RuntimeError(f"Insufficient balance {bal.tao} < required {self.stake_amount_tao} TAO")

        # Load persisted schedule
        self._load_state()

    def _ensure_events_client(self):
        if self.substrate_events is None:
            try:
                self.events_subtensor = self._new_subtensor()
                self.substrate_events = self.events_subtensor.substrate
            except Exception as e:
                self.logger.warning("Recreating events substrate failed: %s", str(e))
                self.substrate_events = self.substrate

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

    def _count_stake_added_for_subnet(self, events, target_subnet: int) -> int:
        """Count StakeAdded for target subnet across possible naming variants."""
        count = 0
        accepted_event_ids = {"StakeAdded"}  # canonical
        accepted_modules = {"SubtensorModule", "Subtensor"}  # pallet naming variants

        for ev in events or []:
            try:
                evval = ev.value if hasattr(ev, "value") else ev
                evt = evval.get("event", {})
                module_id = evt.get("module_id") or evt.get("pallet") or ""
                event_id = evt.get("event_id") or evt.get("event") or ""

                if module_id not in accepted_modules or event_id not in accepted_event_ids:
                    continue

                params = evt.get("attributes") or evt.get("params") or []
                ev_netuid = None
                for p in params:
                    if isinstance(p, dict):
                        name = (p.get("name") or "").lower()
                        if name == "netuid":
                            try:
                                ev_netuid = int(p.get("value"))
                            except Exception:
                                pass
                            break
                        # fallback heuristic
                        if ev_netuid is None and isinstance(p.get("value"), int):
                            ev_netuid = int(p.get("value"))
                if ev_netuid is None:
                    continue
                if ev_netuid == target_subnet:
                    count += 1
            except Exception:
                continue

        return count

    def _best_effort_tip_rao(self) -> Optional[int]:
        if self.tip_tao <= 0:
            return None
        try:
            return int(bt.Balance.from_tao(self.tip_tao).rao)
        except Exception:
            return None

    def _submit_extrinsic_fast(self, call_module: str, call_function_candidates: List[str], params: Dict[str, Any]) -> Optional[str]:
        """Submit extrinsic via dedicated tx substrate with explicit nonce & tip (no waits)."""
        try:
            # Compose call by trying candidate function names
            call = None
            func_used = None
            for func in call_function_candidates:
                try:
                    call = self.substrate_tx.compose_call(
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

            nonce = self.nonce_mgr.next_and_increment() if self.nonce_mgr else None
            tip_rao = self._best_effort_tip_rao()

            extrinsic = self.substrate_tx.create_signed_extrinsic(
                call=call,
                keypair=self.wallet.coldkey,
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
                if self.nonce_mgr:
                    self.nonce_mgr.invalidate()
                    self.nonce_mgr.refresh()
            return None

    def submit_stake(self) -> bool:
        """Submit stake extrinsic quickly with retries (prefer fast path)."""
        amount = bt.Balance.from_tao(self.stake_amount_tao)
        hot_ss58 = self.target_hotkey if self.target_hotkey else self.wallet.hotkey.ss58_address
        netuid = self.subnet_id

        max_retries = int(self.cfg["max_retries"])
        backoff = float(self.cfg["retry_backoff_seconds"])
        max_backoff = float(self.cfg["max_backoff_seconds"])

        for attempt in range(1, max_retries + 1):
            try:
                if self.fast_mode:
                    self.logger.info("Submitting STAKE fast %.8f TAO on subnet %d (attempt %d/%d)", self.stake_amount_tao, netuid, attempt, max_retries)
                    params = {"hotkey": hot_ss58, "amount": int(amount.rao), "netuid": int(netuid)}
                    if self._submit_extrinsic_fast("SubtensorModule", ["add_stake", "addStake"], params):
                        return True
                    self.logger.debug("Fast path failed; falling back to SDK path.")

                # SDK fallback
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

    def submit_unstake(self) -> bool:
        """Submit unstake extrinsic quickly with retries (prefer fast path)."""
        amount = bt.Balance.from_tao(self.stake_amount_tao)
        hot_ss58 = self.target_hotkey if self.target_hotkey else self.wallet.hotkey.ss58_address
        netuid = self.subnet_id

        max_retries = int(self.cfg["max_retries"])
        backoff = float(self.cfg["retry_backoff_seconds"])
        max_backoff = float(self.cfg["max_backoff_seconds"])

        for attempt in range(1, max_retries + 1):
            try:
                if self.fast_mode:
                    self.logger.info("Submitting UNSTAKE fast %.8f TAO on subnet %d (attempt %d/%d)", self.stake_amount_tao, netuid, attempt, max_retries)
                    params = {"hotkey": hot_ss58, "amount": int(amount.rao), "netuid": int(netuid)}
                    if self._submit_extrinsic_fast("SubtensorModule", ["remove_stake", "removeStake"], params):
                        return True
                    self.logger.debug("Fast path failed; falling back to SDK path.")

                # SDK fallback
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
        """Fetch events for block_number, count stakes for target subnet, trigger stake if count >= 2."""
        try:
            if not self.cfg.get("allow_overlap_triggers", False) and self.pending_unstake_block is not None:
                self.logger.debug("Unstake pending for block %s; skip trigger at block %s",
                                  self.pending_unstake_block, block_number)
                return

            events = self._events_for_block(block_number)
            count = self._count_stake_added_for_subnet(events, self.subnet_id)
            self.logger.debug("Block %d: StakeAdded count on subnet %d = %d", block_number, self.subnet_id, count)

            if count >= 2:
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
        """If pending_unstake_block == current_block (or passed), submit unstake."""
        if self.pending_unstake_block is None:
            return
        if current_block >= self.pending_unstake_block:
            self.logger.info("Unstake due at block %d (scheduled: %d)", current_block, self.pending_unstake_block)
            ok = self.submit_unstake()
            if ok:
                self.pending_unstake_block = None
                self._save_state()
            else:
                self.logger.warning("Unstake submission failed; will retry on next block")

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
        self.logger.info("Starting block monitor. Target subnet: %d | Stake amount: %.8f TAO | fast_mode=%s",
                         self.subnet_id, self.stake_amount_tao, self.fast_mode)

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
                    self.logger.info("Subscribing to new block headers (iterator mode or polling fallback)...")
                    # Iterator attempt
                    try:
                        for header in self.substrate.subscribe_block_headers():
                            if self._shutdown:
                                break
                            block_number = self._parse_block_number(header)
                            if block_number is None:
                                continue
                            self.last_analyzed_block = block_number
                            self.logger.debug("New block: %d", block_number)
                            self.execute_unstake(block_number)
                            self.analyze_block(block_number)
                    except TypeError:
                        # Poll fallback
                        self.logger.info("Iterator mode not supported; falling back to polling current block...")
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


# Module-level functions expected by the earlier spec
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
