#!/usr/bin/env python3
"""
Quick unstake script - unstakes from a specific validator
"""

import bittensor as bt

WALLET_NAME = "droplet"
HOTKEY_NAME = "default"  # The mining hotkey
VALIDATOR_HOTKEY = "5E1nK3myeWNWrmffVaH76f2mCFCbe9VcHGwgkfdcD7k3E8D1"
NETUID = 51
NETWORK = "finney"

print("=" * 70)
print("Emergency Unstake Script")
print("=" * 70)

# Initialize wallet
print("\nInitializing wallet...")
wallet = bt.wallet(name=WALLET_NAME, hotkey=HOTKEY_NAME)
print(f"Wallet: {WALLET_NAME}")
print(f"Hotkey: {HOTKEY_NAME}")

# Unlock
print("\nUnlocking wallet...")
wallet.unlock_coldkey()
print("✓ Wallet unlocked")

# Connect
print(f"\nConnecting to {NETWORK}...")
subtensor = bt.Subtensor(network=NETWORK)
print(f"✓ Connected")

# Get current stake
print(f"\nChecking stake on subnet {NETUID}...")
stake_info = subtensor.get_stake_for_coldkey_and_hotkey(
    coldkey_ss58=wallet.coldkeypub.ss58_address,
    hotkey_ss58=VALIDATOR_HOTKEY
)

stake = stake_info.get(NETUID, None)
if not stake or stake.stake.rao == 0:
    print(f"✓ No stake found on subnet {NETUID}")
    print("Nothing to unstake!")
else:
    print(f"Current stake: {stake.stake}")
    
    # Ask for confirmation
    print(f"\n⚠️  About to unstake {stake.stake} from validator")
    confirm = input("Continue? (yes/no): ").strip().lower()
    
    if confirm == 'yes':
        print(f"\nUnstaking {stake.stake}...")
        try:
            success = subtensor.unstake(
                wallet=wallet,
                hotkey_ss58=VALIDATOR_HOTKEY,
                amount=stake.stake,
                netuid=NETUID
            )
            if success:
                print(f"✓ Successfully unstaked {stake.stake}")
            else:
                print("✗ Failed to unstake")
        except Exception as e:
            print(f"✗ Error: {e}")
    else:
        print("Cancelled.")

print("\n" + "=" * 70)
print("Done!")
print("=" * 70)

