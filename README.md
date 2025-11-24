# Bittensor Stake Bot

Simple script to automatically stake and unstake TAO on Bittensor subnets for a full epoch to earn emissions.

## Quick Start

### 1. Install Dependencies

```bash
# Create virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate

# Install requirements
pip install -r requirements.txt
```

### 2. Run the Bot

```bash
python3 stake_bot.py
```

The script will prompt you for:
- Wallet name
- Hotkey name
- Validator hotkey (SS58 address)
- Stake amount in TAO (minimum 0.05)
- Subnet ID
- Network (test/finney)
- Stake mode (epoch or block-by-block)
- Stake duration (number of epochs for epoch mode)
- Continuous mode (y/n)
- Wallet password (secure prompt)

### 3. Run in Background (Optional)

To keep the bot running after closing your terminal:

```bash
# Install screen
apt install screen -y

# Start a screen session
cd /root/stake
source venv/bin/activate
screen -S stake-bot

# Run the bot (it will prompt for password)
python3 stake_bot.py

# Enter your configuration and password
# Once it starts running, press Ctrl+A then D to detach

# To check on it later:
screen -r stake-bot

# To list all sessions:
screen -ls
```

**Benefits of using screen:**
- Bot keeps running even after you disconnect/close terminal
- Easy to reattach and check status anytime
- Simple to stop (Ctrl+C when attached)
- No configuration files needed

## How It Works

The bot performs strategic staking to earn emissions:

### Epoch-Based Staking (Recommended for Emissions)

1. **Stakes** TAO to validator on specified subnet
2. **Holds** for full epoch duration (360 blocks ≈ 72 minutes)
3. **Shows progress** updates every minute
4. **Unstakes** after epoch completes
5. **Repeats** if continuous mode is enabled

### Why Hold for an Epoch?

Bittensor distributes **TAO rewards** at the end of each epoch. To earn emissions:
- **Minimum**: Hold stake for 1 full epoch (360 blocks)
- **Epoch duration**: ~72 minutes
- **Daily epochs**: ~20 per day
- **Rewards**: Distributed to stakers who held through the entire epoch

Staking for just one block doesn't earn rewards!

### Epoch Timeline

```
Block N:       Stake 0.05 TAO
  ↓
Wait ~72 minutes (360 blocks) - Progress updates every minute
  ↓
Block N+360:   Epoch complete - Emissions earned!
  ↓
               Unstake 0.05 TAO
  ↓
60 seconds wait
  ↓
Repeat... (if continuous mode)
```

### Block-by-Block Staking (Rapid Cycling)

For rapid stake/unstake cycles without waiting for epochs:

1. **Stakes** TAO to validator on specified subnet
2. **Waits** for next block (~12 seconds)
3. **Unstakes** immediately on the next block
4. **Repeats** if continuous mode is enabled

⚠️ **Note**: Block-by-block mode does NOT earn emissions! This mode is for testing or other purposes where you need rapid stake/unstake cycles.

### Block Timeline

```
Block N:       Stake 0.05 TAO
  ↓
Wait ~12 seconds (1 block)
  ↓
Block N+1:     Unstake 0.05 TAO
  ↓
60 seconds wait
  ↓
Repeat... (if continuous mode)
```

## Configuration Options

### Stake Mode
- **Epoch mode** (recommended): Stakes and holds for full epoch(s) to earn emissions
  - Choose this to earn TAO rewards
  - Minimum 1 epoch (360 blocks ≈ 72 minutes)
- **Block mode**: Stakes on block N, unstakes on block N+1
  - For rapid testing or specific use cases
  - ⚠️ Does NOT earn emissions!

### Stake Amount
- **Minimum**: 0.05 TAO
- **Recommended**: Start with 0.05-0.1 TAO for testing
- **Note**: Include 5% buffer for transaction fees

### Stake Duration (Epoch Mode Only)
- **1 epoch**: 360 blocks ≈ 72 minutes (minimum for emissions)
- **2 epochs**: 720 blocks ≈ 144 minutes
- **3 epochs**: 1080 blocks ≈ 216 minutes
- **More epochs**: Better rewards accumulation

### Network Selection
- **test**: For testing (recommended first)
- **finney**: Mainnet (real TAO and rewards)

### Continuous Mode
- **Yes**: Keeps staking/unstaking in cycles
- **No**: Runs one cycle then stops

## Finding Validators

Find active validators at:
- **TaoStats**: https://taostats.io/
- **Bittensor Explorer**: https://x.taostats.io/

**Important**: Make sure the validator is active on your chosen subnet!

## Requirements

- Python 3.8+
- Bittensor SDK
- Configured Bittensor wallet with TAO balance
- Sufficient TAO for:
  - Stake amount
  - Transaction fees (≈5% of stake amount)

Check your balance:
```bash
btcli wallet balance --wallet.name [YOUR_WALLET]
```

## Example Session

```bash
$ python3 stake_bot.py

======================================================================
Bittensor Simple Stake Bot
======================================================================

Running in INTERACTIVE MODE
Enter wallet name [default]: droplet
Enter hotkey name [default]: 
Enter validator hotkey (SS58 address): 5D7aRtpmVBKsQRzMA2ioUPL25onJPzBjiFVVt5uPZ3TDsn51
Enter stake amount in TAO [0.05]: 0.05
Enter subnet ID [1]: 51
Enter network (test/finney) [test]: finney

Stake duration options:
  1 epoch  = 360 blocks ≈ 72 minutes (minimum for emissions)
  2 epochs = 720 blocks ≈ 144 minutes (2.4 hours)
  3 epochs = 1080 blocks ≈ 216 minutes (3.6 hours)
Enter number of epochs to stake [1]: 1
Run continuously? (y/n) [n]: y

======================================================================
Configuration:
  Wallet: droplet
  Hotkey: default
  Network: finney
  Validator: 5D7aRtpmVBKsQRzMA2ioUPL25onJPzBjiFVVt5uPZ3TDsn51
  Amount: 0.05 TAO
  Subnet: 51
  Stake Duration: 1 epoch(s) = 360 blocks ≈ 1.2 hours
  Continuous: True
======================================================================

Initializing wallet...
✓ Wallet initialized

Unlocking wallet...
Enter your password: ********
✓ Wallet unlocked

Connecting to finney network...
✓ Connected to finney

Current balance: 0.234600549 TAO

======================================================================
Cycle 1
======================================================================
Current balance: 0.234600549 TAO
Current block: 6899884
Current stake on subnet 51: ‎0.000000000ת‎

Staking 0.05 TAO to subnet 51...
✓ Successfully staked 0.05 TAO

Waiting for next block...
✓ New block: 6899887 (waited 36.2s, 3 blocks)
  Average block time: ~12.1s
Actual staked amount: ‎0.769656036ת‎

💎 Holding stake for 1 epoch(s) (360 blocks)
Start block: 6899887
Target block: 6900247
Estimated time: ~1.2 hours

  Progress: 10.0% (36/360 blocks) | Elapsed: 0.12h | Remaining: ~1.08h
  Progress: 20.0% (72/360 blocks) | Elapsed: 0.24h | Remaining: ~0.96h
  ...
  Progress: 100.0% (360/360 blocks) | Elapsed: 1.20h | Remaining: ~0.00h

✓ Epoch complete! Held for 1.20 hours (360 blocks)

Unstaking ‎0.769656036ת‎ from subnet 51...
✓ Successfully unstaked ‎0.769656036ת‎

✓ Cycle 1 completed successfully

Waiting 60 seconds before next cycle...
```

### Example: Block Mode (Rapid Cycling)

```bash
$ python3 stake_bot.py

======================================================================
Bittensor Simple Stake Bot
======================================================================

Running in INTERACTIVE MODE
Enter wallet name [default]: droplet
Enter hotkey name [default]: 
Enter validator hotkey (SS58 address): 5D7aRtpmVBKsQRzMA2ioUPL25onJPzBjiFVVt5uPZ3TDsn51
Enter stake amount in TAO [0.05]: 0.05
Enter subnet ID [1]: 51
Enter network (test/finney) [test]: finney

Stake mode options:
  1. Epoch mode - Stake and hold for full epoch(s) to earn emissions
  2. Block mode - Stake on block N, unstake on block N+1 (rapid cycling)
Select mode (1 or 2) [1]: 2
Selected: Block-by-block mode (stake then immediately unstake on next block)
Run continuously? (y/n) [n]: y

======================================================================
Configuration:
  Wallet: droplet
  Hotkey: default
  Network: finney
  Validator: 5D7aRtpmVBKsQRzMA2ioUPL25onJPzBjiFVVt5uPZ3TDsn51
  Amount: 0.05 TAO
  Subnet: 51
  Stake Mode: block
  Stake Duration: 1 block (stake then immediate unstake on next block)
  Continuous: True
======================================================================

Initializing wallet...
✓ Wallet initialized

Current balance: 0.234600549 TAO

======================================================================
Cycle 1
======================================================================
Current balance: 0.234600549 TAO
Current block: 6899884

Staking 0.05 TAO to subnet 51...
✓ Successfully staked 0.05 TAO

Waiting for next block...
✓ New block: 6899885 (waited 12.3s, 1 blocks)

⚡ Block mode: Holding stake for 1 block
Staked on block: 6899885
Will unstake on next block: 6899886
✓ Next block reached: 6899886 (held for 12.1s)

Unstaking ‎0.769656036ת‎ from subnet 51...
✓ Successfully unstaked ‎0.769656036ת‎

✓ Cycle 1 completed successfully

Waiting 60 seconds before next cycle...
```

## Managing Background Sessions

### Start in Background
```bash
screen -S stake-bot
python3 stake_bot.py
# Enter configuration and password
# Press Ctrl+A then D to detach
```

### Check on Running Bot
```bash
screen -r stake-bot
# Press Ctrl+A then D to detach again
```

### Stop the Bot
```bash
screen -r stake-bot
# Press Ctrl+C to stop
# Type 'exit' to close the screen session
```

### List All Sessions
```bash
screen -ls
```

## Troubleshooting

### "Wrong password"
Test your password first:
```bash
btcli wallet overview --wallet.name [YOUR_WALLET]
```

### "Insufficient balance"
You need enough TAO for stake amount + fees (≈5% extra).
Check balance:
```bash
btcli wallet balance --wallet.name [YOUR_WALLET]
```

### "Failed to stake/unstake"
- Verify validator is active on the subnet
- Check you have enough balance
- Ensure network is accessible

### Bot keeps restarting itself
- Check that validator hotkey is correct
- Verify you have sufficient balance
- Make sure you're not running multiple instances

### "Currency mismatch" warnings
This is normal - the bot handles Alpha currency conversion automatically.

## Important Notes

### Block Time
- Bittensor produces blocks every **~12 seconds**
- Epochs last **360 blocks** = **~72 minutes**
- Slight variations in block time are normal

### Transaction Fees
- Each stake/unstake costs a small fee
- Bot checks balance before each cycle
- Requires 5% buffer above stake amount

### Emissions
- Only earned by holding stake for full epochs
- Distributed at end of each epoch
- ~20 epochs per day = 20 reward opportunities

### Security
- Password entered interactively (not stored in files)
- Wallet remains encrypted on disk
- Only unlock happens in memory during runtime
- Use screen to run in background safely

## What Gets Logged

The bot shows:
- Current block numbers
- Stake/unstake confirmations
- Progress through epochs
- Balance updates
- Any errors or warnings
- Block timing statistics

## Best Practices

1. **Start with test network** - Verify everything works
2. **Use small amounts** - Test with 0.05-0.1 TAO first
3. **Verify validators** - Check they're active on your subnet
4. **Monitor first epoch** - Watch the full cycle complete
5. **Use screen** - Keep it running in background
6. **Check balance** - Ensure you have enough TAO + fees

## Block Time Reference

| Blocks | Time | Description |
|--------|------|-------------|
| 1 | 12 seconds | One block |
| 5 | 1 minute | |
| 300 | 1 hour | |
| 360 | 72 minutes | **1 epoch** (minimum for rewards) |
| 720 | 2.4 hours | 2 epochs |
| 7200 | 1 day | 20 epochs |

## License

MIT License

## Disclaimer

This software is provided "as is" without warranty. Staking involves financial risk. Always test on test network first. You may lose funds due to transaction fees or network issues. Use at your own risk.

---

**Simple staking for earning Bittensor emissions!**
