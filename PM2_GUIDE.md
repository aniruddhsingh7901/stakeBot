# PM2 Quick Start Guide

This guide helps you run the Bittensor Stake Bot as a background process using PM2.

## Prerequisites

1. **Install Python dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Install PM2 (if not already installed):**
   ```bash
   npm install -g pm2
   ```

3. **Verify PM2 installation:**
   ```bash
   pm2 -v
   ```

## Configuration

### Method 1: Edit ecosystem.config.js directly

1. Open `ecosystem.config.js`
2. Update the `env` section with your values:
   ```javascript
   env: {
     WALLET_NAME: 'your-wallet',
     HOTKEY_NAME: 'your-hotkey',
     VALIDATOR_HOTKEY: '5E2LP6EnZ54m3wS8s1yPvD5c3xo71kQroBw7aUVK32TKeZ5u',
     STAKE_AMOUNT: '0.001',
     NETUID: '1',
     NETWORK: 'test',
     CONTINUOUS: 'true',
     WALLET_PASSWORD: 'your-password',
   }
   ```

### Method 2: Use environment variables (more secure)

1. Set environment variables in your shell:
   ```bash
   export WALLET_NAME="your-wallet"
   export HOTKEY_NAME="your-hotkey"
   export VALIDATOR_HOTKEY="5E2LP6EnZ54m3wS8s1yPvD5c3xo71kQroBw7aUVK32TKeZ5u"
   export STAKE_AMOUNT="0.001"
   export NETUID="1"
   export NETWORK="test"
   export CONTINUOUS="true"
   export WALLET_PASSWORD="your-password"
   ```

2. Start PM2 (it will inherit the environment variables):
   ```bash
   pm2 start ecosystem.config.js
   ```

## Running the Bot

### Start the bot
```bash
pm2 start ecosystem.config.js
```

### View logs (real-time)
```bash
pm2 logs stake-bot
```

### View only error logs
```bash
pm2 logs stake-bot --err
```

### View only output logs
```bash
pm2 logs stake-bot --out
```

### Check status
```bash
pm2 status
```

### Monitor resources
```bash
pm2 monit
```

### Stop the bot
```bash
pm2 stop stake-bot
```

### Restart the bot
```bash
pm2 restart stake-bot
```

### Delete from PM2 (stop and remove)
```bash
pm2 delete stake-bot
```

## Auto-start on System Boot

To make the bot automatically start when your system reboots:

1. **Setup startup script:**
   ```bash
   pm2 startup
   ```
   Follow the command it outputs (usually requires sudo).

2. **Save current PM2 process list:**
   ```bash
   pm2 save
   ```

Now PM2 will automatically start your bot on system reboot!

## Log Management

Logs are stored in the `logs/` directory:
- `logs/stake-bot-out.log` - Standard output
- `logs/stake-bot-error.log` - Error output

### View logs manually
```bash
tail -f logs/stake-bot-out.log
```

### Rotate logs (prevent large files)
```bash
pm2 install pm2-logrotate
pm2 set pm2-logrotate:max_size 10M
pm2 set pm2-logrotate:retain 7
```

## Troubleshooting

### Bot keeps restarting
```bash
# Check error logs
pm2 logs stake-bot --err

# Common causes:
# - Wrong password
# - Invalid validator hotkey
# - Insufficient balance
# - Network connection issues
```

### Bot not starting
```bash
# Test in interactive mode first
python3 stake_bot.py

# Check PM2 logs
pm2 logs stake-bot

# Check PM2 status
pm2 status
```

### Update configuration
```bash
# 1. Stop the bot
pm2 stop stake-bot

# 2. Edit ecosystem.config.js
nano ecosystem.config.js

# 3. Restart with new config
pm2 restart stake-bot
```

## Security Best Practices

1. **Never commit passwords:** Add `.env` to `.gitignore`
2. **Use environment variables:** Set at system level instead of in config files
3. **Secure log files:** Logs contain transaction information
4. **Test first:** Always test on test network before mainnet
5. **Monitor regularly:** Check logs and status frequently
6. **Use keyfiles:** Prefer Bittensor keyfiles over plaintext passwords

## Testing on Test Network

Before running on mainnet (finney):

1. Set `NETWORK: 'test'` in config
2. Use small stake amount (0.001 TAO)
3. Run for a few cycles to ensure it works
4. Check logs for any errors

## Switching to Mainnet

Once tested on test network:

1. Stop the bot: `pm2 stop stake-bot`
2. Update config:
   - Change `NETWORK` to `'finney'`
   - Update `STAKE_AMOUNT` (mainnet usually requires 1 TAO minimum)
3. Restart: `pm2 restart stake-bot`
4. Monitor closely: `pm2 logs stake-bot`

## Common Commands Cheat Sheet

```bash
# Start
pm2 start ecosystem.config.js

# Logs
pm2 logs stake-bot
pm2 logs stake-bot --err
pm2 logs stake-bot --lines 100

# Status & Monitoring
pm2 status
pm2 monit
pm2 info stake-bot

# Control
pm2 stop stake-bot
pm2 restart stake-bot
pm2 delete stake-bot

# Startup
pm2 startup
pm2 save
pm2 resurrect

# Logs Management
pm2 flush stake-bot  # Clear logs
pm2 reloadLogs       # Reload log config
```

## Getting Help

If you encounter issues:

1. Check the logs: `pm2 logs stake-bot`
2. Try interactive mode: `python3 stake_bot.py`
3. Verify configuration in `ecosystem.config.js`
4. Check balance: `btcli wallet balance --wallet.name [YOUR_WALLET]`
5. Verify validator is active on the subnet

---

**Happy staking!** 🚀

