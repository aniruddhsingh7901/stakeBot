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

import os
import sys
import time

import bittensor as bt


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
        STAKE_AMOUNT = float(os.getenv('STAKE_AMOUNT', '0.01'))
        NETUID = int(os.getenv('NETUID', '1'))
        NETWORK = os.getenv('NETWORK', 'test')
        STAKE_MODE = os.getenv('STAKE_MODE', 'epoch').lower()  # 'epoch' or 'block'
        
        if STAKE_MODE == 'block':
            BLOCKS_TO_WAIT = 1  # Just wait for next block
            EPOCHS_TO_STAKE = 0
        else:
            EPOCHS_TO_STAKE = int(os.getenv('EPOCHS_TO_STAKE', '1'))
            BLOCKS_TO_WAIT = EPOCHS_TO_STAKE * 360  # 360 blocks per epoch (72 minutes)
        
        CONTINUOUS = os.getenv('CONTINUOUS', 'false').lower() in ['true', 'yes', '1', 'y']
        WALLET_PASSWORD = os.getenv('WALLET_PASSWORD')
    else:
        print("\nRunning in INTERACTIVE MODE")
        WALLET_NAME = input("Enter wallet name [default]: ").strip() or "default"
        HOTKEY_NAME = input("Enter hotkey name [default]: ").strip() or "default"
        VALIDATOR_HOTKEY = input("Enter validator hotkey (SS58 address): ").strip()
        STAKE_AMOUNT = float(input("Enter stake amount in TAO [0.01]: ").strip() or "0.01")
        NETUID = int(input("Enter subnet ID [1]: ").strip() or "1")
        NETWORK = input("Enter network (test/finney) [test]: ").strip() or "test"
        
        # Ask for stake mode
        print("\nStake mode options:")
        print("  1. Epoch mode - Stake and hold for full epoch(s) to earn emissions")
        print("  2. Block mode - Stake on block N, unstake on block N+1 (rapid cycling)")
        mode_input = input("Select mode (1 or 2) [1]: ").strip() or "1"
        
        if mode_input == "2":
            STAKE_MODE = 'block'
            BLOCKS_TO_WAIT = 1
            EPOCHS_TO_STAKE = 0
            print("Selected: Block-by-block mode (stake then immediately unstake on next block)")
        else:
            STAKE_MODE = 'epoch'
            # Ask for stake duration
            print("\nStake duration options:")
            print("  1 epoch  = 360 blocks ≈ 72 minutes (minimum for emissions)")
            print("  2 epochs = 720 blocks ≈ 144 minutes (2.4 hours)")
            print("  3 epochs = 1080 blocks ≈ 216 minutes (3.6 hours)")
            duration_input = input("Enter number of epochs to stake [1]: ").strip() or "1"
            EPOCHS_TO_STAKE = int(duration_input)
            BLOCKS_TO_WAIT = EPOCHS_TO_STAKE * 360  # 360 blocks per epoch
        
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
    print(f"  Stake Mode: {STAKE_MODE}")
    if STAKE_MODE == 'block':
        print(f"  Stake Duration: 1 block (stake then immediate unstake on next block)")
    else:
        print(f"  Stake Duration: {EPOCHS_TO_STAKE} epoch(s) = {BLOCKS_TO_WAIT} blocks ≈ {BLOCKS_TO_WAIT * 12 / 3600:.1f} hours")
    print(f"  Continuous: {CONTINUOUS}")
    print("=" * 70)
    
    # Initialize wallet
    print("\nInitializing wallet...")
    wallet = bt.wallet(name=WALLET_NAME, hotkey=HOTKEY_NAME)
    wallet.create_if_non_existent()
    
    # Unlock coldkey
    print("\nUnlocking wallet...")
    try:
        if WALLET_PASSWORD:
            # Try with password from environment variable (PM2 mode)
            import os as _os
            _os.environ['BT_WALLET_PASSWORD'] = WALLET_PASSWORD
            wallet.unlock_coldkey()
        else:
            # Prompt for password (interactive mode) or use unencrypted key
            wallet.unlock_coldkey()
        print("✓ Wallet unlocked")
    except Exception as e:
        # If unlock fails, wallet might already be unencrypted
        print(f"✓ Wallet loaded (unencrypted or already unlocked)")
    
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
            
            # In block mode, skip slow balance check every cycle (only check first time)
            if STAKE_MODE == 'block' and cycle > 1:
                # Skip balance check to save time in rapid cycling
                pass
            else:
                # Check balance before each cycle
                balance = subtensor.get_balance(wallet.coldkey.ss58_address)
                print(f"Current balance: {balance.tao} TAO")
                
                # Check if we have enough balance (with some buffer for fees)
                required_balance = STAKE_AMOUNT * 1.05  # 5% buffer for fees
                if balance.tao < required_balance:
                    print(f"✗ Insufficient balance: {balance.tao} TAO < {required_balance} TAO (needed with fee buffer)")
                    print(f"Transaction fees have reduced balance below threshold")
                    break
            
            # Get current block
            current_block = subtensor.get_current_block()
            print(f"Block {current_block}")
            
            # In block mode, skip the slow stake query - just stake and unstake fast!
            if STAKE_MODE == 'block':
                # Skip stake checking to save time
                stake_before_amount = bt.Balance.from_rao(0)
            else:
                # Get stake before staking for the specific subnet (only in epoch mode)
                print(f"Checking current stake on subnet {NETUID}...")
                stake_info_before = subtensor.get_stake_for_coldkey_and_hotkey(
                    coldkey_ss58=wallet.coldkeypub.ss58_address,
                    hotkey_ss58=VALIDATOR_HOTKEY
                )
                stake_before = stake_info_before.get(NETUID, None)
                if stake_before:
                    print(f"Current stake on subnet {NETUID}: {stake_before.stake}")
                    stake_before_amount = stake_before.stake
                else:
                    print(f"No existing stake on subnet {NETUID}")
                    stake_before_amount = bt.Balance.from_rao(0)
            
            # Stake
            print(f"Staking {STAKE_AMOUNT} TAO...")
            try:
                success = subtensor.add_stake(
                    wallet=wallet,
                    hotkey_ss58=VALIDATOR_HOTKEY,
                    amount=amount,
                    netuid=NETUID
                )
                if success:
                    print(f"✓ Staked")
                else:
                    print("✗ Failed to stake")
                    break
            except Exception as e:
                print(f"✗ Error staking: {e}")
                break
            
            # Handle different stake modes
            if STAKE_MODE == 'block':
                # Block mode: Get stake and unstake immediately!
                print(f"Getting stake...")
            else:
                # Epoch mode: Wait for next block first
                print(f"\nWaiting for next block...")
                block_wait_start = time.time()
                while True:
                    new_block = subtensor.get_current_block()
                    if new_block > current_block:
                        block_wait_time = time.time() - block_wait_start
                        blocks_elapsed = new_block - current_block
                        print(f"✓ New block: {new_block} (waited {block_wait_time:.1f}s, {blocks_elapsed} blocks)")
                        if blocks_elapsed > 1:
                            avg_block_time = block_wait_time / blocks_elapsed
                            print(f"  Average block time: ~{avg_block_time:.1f}s")
                        break
                    time.sleep(1)
                
                # Epoch mode: Wait for full epoch duration
                start_block = new_block
                target_block = start_block + BLOCKS_TO_WAIT
                print(f"\n💎 Holding stake for {EPOCHS_TO_STAKE} epoch(s) ({BLOCKS_TO_WAIT} blocks)")
                print(f"Start block: {start_block}")
                print(f"Target block: {target_block}")
                print(f"Estimated time: ~{BLOCKS_TO_WAIT * 12 / 3600:.1f} hours")
                
                epoch_start_time = time.time()
                last_update = time.time()
                
                while True:
                    current = subtensor.get_current_block()
                    blocks_remaining = target_block - current
                    
                    if current >= target_block:
                        elapsed_time = time.time() - epoch_start_time
                        print(f"\n✓ Epoch complete! Held for {elapsed_time / 3600:.2f} hours ({current - start_block} blocks)")
                        break
                    
                    # Update progress every 60 seconds
                    if time.time() - last_update >= 60:
                        blocks_done = current - start_block
                        progress = (blocks_done / BLOCKS_TO_WAIT) * 100
                        elapsed = time.time() - epoch_start_time
                        estimated_total = (elapsed / blocks_done) * BLOCKS_TO_WAIT if blocks_done > 0 else 0
                        remaining_time = estimated_total - elapsed
                        
                        print(f"  Progress: {progress:.1f}% ({blocks_done}/{BLOCKS_TO_WAIT} blocks) | "
                              f"Elapsed: {elapsed/3600:.2f}h | Remaining: ~{remaining_time/3600:.2f}h")
                        last_update = time.time()
                    
                    time.sleep(10)  # Check every 10 seconds
            
            # Get actual staked amount after staking
            if STAKE_MODE == 'block':
                # Block mode: Get stake immediately after staking
                stake_info_after = subtensor.get_stake_for_coldkey_and_hotkey(
                    coldkey_ss58=wallet.coldkeypub.ss58_address,
                    hotkey_ss58=VALIDATOR_HOTKEY
                )
                stake_after = stake_info_after.get(NETUID, None)
                if stake_after:
                    actual_staked = stake_after.stake
                    print(f"Unstaking {actual_staked}...")
                else:
                    print("✗ No stake found")
                    break
            else:
                # Epoch mode: Query to get exact amount
                stake_info_after = subtensor.get_stake_for_coldkey_and_hotkey(
                    coldkey_ss58=wallet.coldkeypub.ss58_address,
                    hotkey_ss58=VALIDATOR_HOTKEY
                )
                stake_after = stake_info_after.get(NETUID, None)
                if stake_after:
                    stake_after_amount = stake_after.stake
                    actual_staked = bt.Balance.from_rao(stake_after_amount.rao - stake_before_amount.rao)
                    actual_staked = actual_staked.set_unit(NETUID)
                    print(f"Actual staked amount: {actual_staked}")
                else:
                    print("✗ Error: Could not get stake info after staking")
                    break
                
                print(f"\nUnstaking {actual_staked} from subnet {NETUID}...")
            try:
                success = subtensor.unstake(
                    wallet=wallet,
                    hotkey_ss58=VALIDATOR_HOTKEY,
                    amount=actual_staked,
                    netuid=NETUID
                )
                if success:
                    if STAKE_MODE == 'block':
                        print(f"✓ Unstaked | Cycle {cycle} done")
                    else:
                        print(f"✓ Successfully unstaked {actual_staked}")
                else:
                    print("✗ Failed to unstake")
                    break
            except Exception as e:
                print(f"✗ Error unstaking: {e}")
                break
            
            if STAKE_MODE != 'block':
                print(f"\n✓ Cycle {cycle} completed successfully")
            
            # Check if we should continue
            if not CONTINUOUS:
                print("\nSingle cycle mode - stopping")
                break
            
            # Wait before next cycle (allows balance to update and avoids spam)
            if STAKE_MODE == 'block':
                # Block mode: NO WAIT - go immediately to next cycle
                print(f"Starting next cycle immediately...")
            else:
                wait_time = 60  # Longer wait for epoch mode
                print(f"\nWaiting {wait_time} seconds before next cycle (allows balance to update)...")
                time.sleep(wait_time)
            
            cycle += 1
            
    except KeyboardInterrupt:
        print("\n\nBot stopped by user")
    
    print("\n" + "=" * 70)
    print("Bot stopped")
    print("=" * 70)

if __name__ == "__main__":
    main()
