#!/usr/bin/env python3
"""
Simple Bittensor Stake Bot
Stake and unstake on subnet block-by-block

Based on: https://gist.github.com/josephjacks/32a4b1db0c191dff26687b6b5da1f984
Simplified for single subnet stake/unstake operations

Usage:
    Interactive mode: python3 stake_bot.py
    PM2 mode: pm2 start ecosystem.config.js

The script can work in two modes:
1. Interactive: Prompts for configuration
2. Environment variables: Uses environment variables (for PM2)
"""

import bittensor as bt
import time
import sys
import os

def main():
    print("=" * 70)
    print("Bittensor Simple Stake Bot")
    print("=" * 70)
    
    # Check if running in environment mode (PM2) or interactive mode
    use_env = os.getenv('VALIDATOR_HOTKEY') is not None
    
    # Configuration
    if use_env:
        print("\nRunning in ENVIRONMENT MODE (PM2)")
        WALLET_NAME = os.getenv('WALLET_NAME', 'default')
        HOTKEY_NAME = os.getenv('HOTKEY_NAME', 'default')
        VALIDATOR_HOTKEY = os.getenv('VALIDATOR_HOTKEY', '')
        STAKE_AMOUNT = float(os.getenv('STAKE_AMOUNT', '0.001'))
        NETUID = int(os.getenv('NETUID', '1'))
        NETWORK = os.getenv('NETWORK', 'test')
        CONTINUOUS = os.getenv('CONTINUOUS', 'false').lower() in ['true', 'yes', '1', 'y']
        WALLET_PASSWORD = os.getenv('WALLET_PASSWORD')
    else:
        print("\nRunning in INTERACTIVE MODE")
        WALLET_NAME = input("Enter wallet name [default]: ").strip() or "default"
        HOTKEY_NAME = input("Enter hotkey name [default]: ").strip() or "default"
        VALIDATOR_HOTKEY = input("Enter validator hotkey (SS58 address): ").strip()
        STAKE_AMOUNT = float(input("Enter stake amount in TAO [0.001]: ").strip() or "0.001")
        NETUID = int(input("Enter subnet ID [1]: ").strip() or "1")
        NETWORK = input("Enter network (test/finney) [test]: ").strip() or "test"
        CONTINUOUS = input("Run continuously? (y/n) [n]: ").strip().lower() == 'y'
        WALLET_PASSWORD = None
    
    if not VALIDATOR_HOTKEY:
        print("ERROR: Validator hotkey is required!")
        sys.exit(1)
    
    print("\n" + "=" * 70)
    print("Configuration:")
    print(f"  Wallet: {WALLET_NAME}")
    print(f"  Hotkey: {HOTKEY_NAME}")
    print(f"  Network: {NETWORK}")
    print(f"  Validator: {VALIDATOR_HOTKEY}")
    print(f"  Amount: {STAKE_AMOUNT} TAO")
    print(f"  Subnet: {NETUID}")
    print(f"  Continuous: {CONTINUOUS}")
    print("=" * 70)
    
    # Initialize wallet
    print("\nInitializing wallet...")
    wallet = bt.wallet(name=WALLET_NAME, hotkey=HOTKEY_NAME)
    wallet.create_if_non_existent()
    
    # Unlock coldkey
    print("\nUnlocking wallet...")
    if WALLET_PASSWORD:
        # Use password from environment variable (PM2 mode)
        wallet.unlock_coldkey(password=WALLET_PASSWORD)
    else:
        # Prompt for password (interactive mode)
        wallet.unlock_coldkey()
    print("✓ Wallet unlocked")
    
    # Connect to network
    print(f"\nConnecting to {NETWORK} network...")
    subtensor = bt.Subtensor(network=NETWORK)
    print(f"✓ Connected to {NETWORK}")
    
    # Check balance
    balance = subtensor.get_balance(wallet.coldkey.ss58_address)
    print(f"\nCurrent balance: {balance.tao} TAO")
    
    if balance.tao < STAKE_AMOUNT:
        print(f"ERROR: Insufficient balance ({balance.tao} TAO < {STAKE_AMOUNT} TAO required)")
        sys.exit(1)
    
    # Convert amount
    amount = bt.Balance.from_tao(STAKE_AMOUNT)
    
    # Main loop
    cycle = 1
    try:
        while True:
            print("\n" + "=" * 70)
            print(f"Cycle {cycle}")
            print("=" * 70)
            
            # Get current block
            current_block = subtensor.get_current_block()
            print(f"Current block: {current_block}")
            
            # Stake
            print(f"\nStaking {STAKE_AMOUNT} TAO to subnet {NETUID}...")
            try:
                success = subtensor.add_stake(
                    wallet=wallet,
                    hotkey_ss58=VALIDATOR_HOTKEY,
                    amount=amount,
                    netuid=NETUID
                )
                if success:
                    print(f"✓ Successfully staked {STAKE_AMOUNT} TAO")
                else:
                    print("✗ Failed to stake")
                    break
            except Exception as e:
                print(f"✗ Error staking: {e}")
                break
            
            # Wait for next block
            print(f"\nWaiting for next block...")
            while True:
                new_block = subtensor.get_current_block()
                if new_block > current_block:
                    print(f"✓ New block: {new_block}")
                    break
                time.sleep(1)
            
            # Unstake
            print(f"\nUnstaking {STAKE_AMOUNT} TAO from subnet {NETUID}...")
            try:
                success = subtensor.unstake(
                    wallet=wallet,
                    hotkey_ss58=VALIDATOR_HOTKEY,
                    amount=amount,
                    netuid=NETUID
                )
                if success:
                    print(f"✓ Successfully unstaked {STAKE_AMOUNT} TAO")
                else:
                    print("✗ Failed to unstake")
                    break
            except Exception as e:
                print(f"✗ Error unstaking: {e}")
                break
            
            print(f"\n✓ Cycle {cycle} completed successfully")
            
            # Check if we should continue
            if not CONTINUOUS:
                print("\nSingle cycle mode - stopping")
                break
            
            # Wait before next cycle
            print("\nWaiting 60 seconds before next cycle...")
            time.sleep(60)
            cycle += 1
            
    except KeyboardInterrupt:
        print("\n\nBot stopped by user")
    
    print("\n" + "=" * 70)
    print("Bot stopped")
    print("=" * 70)

if __name__ == "__main__":
    main()
