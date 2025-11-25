#!/usr/bin/env python3
"""
Quick script to unstake all currently staked Alpha on a subnet
"""

import bittensor as bt

# Configuration
WALLET_NAME = "Anniruddh"
HOTKEY_NAME = "Anniruddh-1"
VALIDATOR_HOTKEY = "5E2LP6EnZ54m3wS8s1yPvD5c3xo71kQroBw7aUVK32TKeZ5u"
NETUID = 31
NETWORK = "finney"

print("=" * 70)
print("Unstake All - Subnet 51")
print("=" * 70)

# Initialize wallet
print("\nInitializing wallet...")
wallet = bt.wallet(name=WALLET_NAME, hotkey=HOTKEY_NAME)
print("✓ Wallet initialized")

# Unlock coldkey
print("\nUnlocking wallet...")
wallet.unlock_coldkey()
print("✓ Wallet unlocked")

# Connect to network
print(f"\nConnecting to {NETWORK} network...")
subtensor = bt.Subtensor(network=NETWORK)
print(f"✓ Connected to {NETWORK}")

# Get current balance
balance = subtensor.get_balance(wallet.coldkey.ss58_address)
print(f"\nCurrent TAO balance: {balance.tao} TAO")

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
    print(f"✓ Found stake: {stake.stake}")
    
    # Unstake all
    print(f"\nUnstaking all {stake.stake} from subnet {NETUID}...")
    try:
        success = subtensor.unstake(
            wallet=wallet,
            hotkey_ss58=VALIDATOR_HOTKEY,
            amount=stake.stake,
            netuid=NETUID
        )
        if success:
            print(f"✓ Successfully unstaked {stake.stake}")
            
            # Check new balance
            new_balance = subtensor.get_balance(wallet.coldkey.ss58_address)
            print(f"\nNew TAO balance: {new_balance.tao} TAO")
            print(f"Recovered: {new_balance.tao - balance.tao} TAO")
        else:
            print("✗ Failed to unstake")
    except Exception as e:
        print(f"✗ Error: {e}")

print("\n" + "=" * 70)
print("Done!")
print("=" * 70)

