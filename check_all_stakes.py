#!/usr/bin/env python3
"""
Check all current stakes across all subnets
"""

import bittensor as bt

WALLET_NAME = "droplet"
NETWORK = "finney"

print("=" * 70)
print("Check All Stakes")
print("=" * 70)

# Initialize wallet
print(f"\nWallet: {WALLET_NAME}")
wallet = bt.wallet(name=WALLET_NAME)

# Connect
print(f"\nConnecting to {NETWORK}...")
subtensor = bt.Subtensor(network=NETWORK)
print(f"✓ Connected")

# Get balance
balance = subtensor.get_balance(wallet.coldkey.ss58_address)
print(f"\nColdkey Address: {wallet.coldkeypub.ss58_address}")
print(f"Current Balance: {balance.tao} TAO")

# Check both hotkeys
hotkeys = ["default", "df2"]

for hotkey_name in hotkeys:
    print(f"\n{'=' * 70}")
    print(f"Hotkey: {hotkey_name}")
    print(f"{'=' * 70}")
    
    try:
        hk_wallet = bt.wallet(name=WALLET_NAME, hotkey=hotkey_name)
        print(f"Address: {hk_wallet.hotkey.ss58_address}")
        
        # Check common validators
        validators = [
            "5E1nK3myeWNWrmffVaH76f2mCFCbe9VcHGwgkfdcD7k3E8D1",  # Your validator
            "5D7aRtpmVBKsQRzMA2ioUPL25onJPzBjiFVVt5uPZ3TDsn51",  # Another one you used
        ]
        
        for val in validators:
            stake_info = subtensor.get_stake_for_coldkey_and_hotkey(
                coldkey_ss58=wallet.coldkeypub.ss58_address,
                hotkey_ss58=val
            )
            
            # Check all subnets
            for netuid, info in stake_info.items():
                if info.stake.rao > 0:
                    print(f"  Subnet {netuid}: {info.stake} staked to {val[:10]}...")
    except Exception as e:
        print(f"  Error checking {hotkey_name}: {e}")

print(f"\n{'=' * 70}")
print("Summary Complete")
print(f"{'=' * 70}")

