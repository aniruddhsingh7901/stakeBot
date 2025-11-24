# Block-by-Block Staking Feature

## Overview
Added a new "block mode" option to the stake bot that allows rapid stake/unstake cycles.

## New Feature: Block Mode

### What It Does
- Stakes TAO on block N
- Waits for next block (~12 seconds)
- Unstakes immediately on block N+1
- Repeats if continuous mode is enabled

### When to Use
- **Testing**: Quick validation of staking functionality
- **Rapid cycling**: When you need fast stake/unstake cycles
- **Non-emission scenarios**: Use cases that don't require earning emissions

### ⚠️ Important Notes
- **Does NOT earn emissions** - Bittensor emissions require holding stake for a full epoch (360 blocks)
- For earning rewards, use **Epoch Mode** instead
- Transaction fees still apply on each stake/unstake operation

## Configuration

### Interactive Mode
When running `python3 stake_bot.py`, you'll see:

```
Stake mode options:
  1. Epoch mode - Stake and hold for full epoch(s) to earn emissions
  2. Block mode - Stake on block N, unstake on block N+1 (rapid cycling)
Select mode (1 or 2) [1]:
```

Select option **2** for block mode.

### PM2/Environment Mode
In `ecosystem.config.js`, set:

```javascript
STAKE_MODE: 'block',  // Options: 'epoch' or 'block'
```

### Environment Variables
```bash
export STAKE_MODE=block  # 'epoch' (default) or 'block'
```

## Comparison

| Feature | Epoch Mode | Block Mode |
|---------|-----------|------------|
| Hold Duration | 360 blocks (~72 min) | 1 block (~12 sec) |
| Earns Emissions | ✅ Yes | ❌ No |
| Best For | Earning TAO rewards | Testing, rapid cycles |
| Cycle Time | ~72 minutes | ~12 seconds |
| Continuous Loops/Day | ~20 cycles | ~7,200 cycles |

## Files Modified

1. **stake_bot.py**
   - Added `STAKE_MODE` configuration option
   - Added block mode logic in wait loop
   - Updated output messages for both modes

2. **ecosystem.config.js**
   - Added `STAKE_MODE` environment variable
   - Updated comments with new option

3. **README.md**
   - Added block mode documentation
   - Added block mode example session
   - Updated configuration options section

## Example Output

```
⚡ Block mode: Holding stake for 1 block
Staked on block: 6899885
Will unstake on next block: 6899886
✓ Next block reached: 6899886 (held for 12.1s)
```

## Usage Examples

### Quick Test Run (Block Mode)
```bash
python3 stake_bot.py
# Select mode: 2 (Block mode)
# Select continuous: n
# Result: One stake/unstake cycle in ~24 seconds
```

### Continuous Testing (Block Mode)
```bash
python3 stake_bot.py
# Select mode: 2 (Block mode)  
# Select continuous: y
# Result: Continuous rapid cycling
```

### Earn Emissions (Epoch Mode - Recommended)
```bash
python3 stake_bot.py
# Select mode: 1 (Epoch mode)
# Select epochs: 1
# Select continuous: y
# Result: Stakes for full epochs to earn rewards
```

