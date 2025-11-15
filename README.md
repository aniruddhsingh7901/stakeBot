# Bittensor Stake Bot

Simple script to automatically stake and unstake TAO on Bittensor subnets, block-by-block.

> **🚀 New to PM2?** Check out [QUICKSTART.md](QUICKSTART.md) for step-by-step instructions!

## 🚀 Quick Start

### Interactive Mode (CLI)

```bash
# Install dependencies
pip install -r requirements.txt

# Run the script
python3 stake_bot.py
```

The script will prompt you for configuration.

### PM2 Mode (Background Process)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Install PM2 (if not already installed)
npm install -g pm2

# 3. IMPORTANT: Edit ecosystem.config.js
# Open ecosystem.config.js in a text editor and update the env section:
#   - Set WALLET_NAME (your wallet name)
#   - Set HOTKEY_NAME (your hotkey name)  
#   - Set VALIDATOR_HOTKEY (validator SS58 address) - REQUIRED!
#   - Set STAKE_AMOUNT (amount to stake)
#   - Set NETUID (subnet ID)
#   - Set NETWORK ('test' or 'finney')
#   - Set CONTINUOUS ('true' for continuous operation)
#   - Set WALLET_PASSWORD (your wallet password) - REQUIRED!

# 4. Start the bot with PM2
pm2 start ecosystem.config.js

# 5. View logs
pm2 logs stake-bot

# Stop the bot
pm2 stop stake-bot

# Restart the bot
pm2 restart stake-bot

# View status
pm2 status

# Make PM2 start on system boot (optional)
pm2 startup
pm2 save
```

**⚠️ IMPORTANT:** You MUST edit `ecosystem.config.js` before starting the bot! At minimum, set:
- `VALIDATOR_HOTKEY` - Your validator's SS58 address
- `WALLET_PASSWORD` - Your wallet password

## ⚙️ Configuration

### Interactive Mode

The script will prompt you for:
- Wallet name
- Hotkey name
- Validator hotkey address
- Stake amount
- Subnet ID
- Network (test/finney)
- Continuous mode (y/n)
- Wallet password (secure prompt)

### PM2 Mode

**Before starting the bot with PM2, you MUST edit `ecosystem.config.js`:**

1. Open `ecosystem.config.js` in your text editor
2. Find the `env` section (around line 10)
3. Update these values with your configuration:

```javascript
env: {
  WALLET_NAME: 'your-wallet',              // Replace with your wallet name
  HOTKEY_NAME: 'your-hotkey',              // Replace with your hotkey name
  VALIDATOR_HOTKEY: '5E2LP...',            // REQUIRED: Replace with validator address
  STAKE_AMOUNT: '0.001',                   // Amount to stake (test with 0.001)
  NETUID: '1',                             // Subnet ID
  NETWORK: 'test',                         // 'test' or 'finney' for mainnet
  CONTINUOUS: 'true',                      // 'true' for continuous operation
  WALLET_PASSWORD: 'your-password',        // REQUIRED: Your wallet password
}
```

4. Save the file
5. Then run: `pm2 start ecosystem.config.js`

**⚠️ Required Fields:**
- `VALIDATOR_HOTKEY` - Must be a valid SS58 address
- `WALLET_PASSWORD` - Your wallet password (or leave empty and use keyfile)

**Security Note:** For production, consider using:
- Encrypted keyfiles instead of plaintext passwords
- Environment variables set at system level
- PM2 secrets management

## 📋 Requirements

- Python 3.8+
- Bittensor SDK
- Configured Bittensor wallet with TAO balance
- PM2 (optional, for background process)

## 📁 Project Structure

```
stake/
├── stake_bot.py          # Main bot script
├── ecosystem.config.js   # PM2 configuration
├── requirements.txt      # Python dependencies
├── logs/                 # PM2 logs directory (auto-created)
└── README.md
```

## ⚙️ How It Works

The bot automatically:

1. **Connects** to Bittensor network
2. **Unlocks** your wallet (prompts for password)
3. **Stakes** TAO to validator on specified subnet
4. **Waits** for next block (~12 seconds)
5. **Unstakes** TAO from validator
6. **Repeats** if continuous mode is enabled

### Block-by-Block Operation

```
Block N:     Stake 0.001 TAO
  ↓
~12 seconds (wait for next block)
  ↓
Block N+1:   Unstake 0.001 TAO
  ↓
60 seconds (if continuous)
  ↓
Repeat...
```

## 💡 Example Usage

### Single Cycle (Test)

**Interactive Mode:**
```bash
python3 stake_bot.py

# Prompts:
Enter wallet name: my-wallet
Enter hotkey name: my-hotkey
Enter validator hotkey: 5E2LP6EnZ54m3wS8s1yPvD5c3xo71kQroBw7aUVK32TKeZ5u
Enter stake amount in TAO: 0.001
Enter subnet ID: 1
Enter network: test
Run continuously? (y/n): n
Enter your password: ********
```

**PM2 Mode:**
```bash
# 1. Edit ecosystem.config.js
#    - Set your wallet name, hotkey, validator address
#    - Set CONTINUOUS: 'false' for single cycle
#    - Set WALLET_PASSWORD
# 2. Start with PM2
pm2 start ecosystem.config.js
pm2 logs stake-bot
```

### Continuous Mode (Production)

**Interactive Mode:**
```bash
python3 stake_bot.py

# When prompted, enter 'y' for continuous mode
# The bot will run forever until you press Ctrl+C
```

**PM2 Mode (Recommended for 24/7 operation):**
```bash
# 1. Edit ecosystem.config.js
#    - Set your wallet name, hotkey, validator address
#    - Set CONTINUOUS: 'true' for continuous operation
#    - Set WALLET_PASSWORD
# 2. Start with PM2
pm2 start ecosystem.config.js

# Bot runs in background
# Auto-restarts on failure
# Logs to ./logs/ directory
```

## 🔍 Finding Validators

Find active validators at:
- **TaoStats:** https://taostats.io/
- **Bittensor Explorer:** https://x.taostats.io/

Make sure the validator is active on your chosen subnet!

## ⚠️ Important Notes

### Minimum Stake Amounts

- **Test network:** 0.001 TAO minimum
- **Finney (mainnet):** Usually 1 TAO minimum (varies by subnet)

If you get `AmountTooLow` error, increase the stake amount.

### Network Selection

- **test:** For testing (recommended first)
- **finney:** Mainnet (real TAO)

### Balance Requirements

Make sure you have enough TAO:
- For staking amount
- Plus transaction fees

Check balance:
```bash
btcli wallet balance --wallet.name [YOUR_WALLET]
```

## 🛑 Stopping the Bot

**Interactive Mode:**
Press `Ctrl+C` to stop the bot gracefully.

**PM2 Mode:**
```bash
pm2 stop stake-bot     # Stop the bot
pm2 delete stake-bot   # Stop and remove from PM2
```

## 📊 Monitoring (PM2)

```bash
# View real-time logs
pm2 logs stake-bot

# View status
pm2 status

# Monitor resources
pm2 monit

# View specific log file
tail -f logs/stake-bot-out.log
```

## 🔒 Security

### Interactive Mode
- ✅ Password entered interactively (not stored)
- ✅ Runs in foreground (you see everything)
- ✅ Easy to stop with Ctrl+C

### PM2 Mode
- ⚠️ Password stored in config or environment variable
- ✅ Runs as background process
- ✅ Auto-restart on failure
- ✅ Logs all activity

**Best Practices:**
- Always test on test network first
- Use small amounts for testing
- Verify validator addresses
- Never share your password
- For PM2: Use encrypted keyfiles or system-level environment variables
- Keep logs secure (contains transaction info)
- Use PM2 startup to ensure bot restarts after reboot

## 🛠️ Troubleshooting

### "Wrong password"
Make sure you're entering the correct wallet password. Test it first:
```bash
btcli wallet overview --wallet.name [YOUR_WALLET]
```

### "Insufficient balance"
Check your balance:
```bash
btcli wallet balance --wallet.name [YOUR_WALLET]
```

### "AmountTooLow"
Increase the stake amount. Finney mainnet typically requires 1 TAO minimum.

### "Failed to stake/unstake"
- Check validator is active on the subnet
- Verify you have enough balance
- Make sure network is accessible

### PM2: Bot keeps restarting
Check logs for errors:
```bash
pm2 logs stake-bot --err
```
Common issues:
- Wrong password in config
- Invalid validator hotkey
- Insufficient balance

### PM2: Bot not starting
```bash
# Check PM2 logs
pm2 logs stake-bot

# Try running interactively first to test
python3 stake_bot.py

# Check PM2 status
pm2 status
```

## 📚 What is Block Time?

Bittensor produces a new block approximately every **12 seconds**. The script:
- Stakes in block N
- Waits ~12 seconds for block N+1
- Unstakes in block N+1

This ensures transactions are confirmed in separate blocks.

## 📄 License

MIT License

## ⚠️ Disclaimer

This software is provided "as is" without warranty. Staking involves financial risk. Always test on test network first. Use at your own risk.

---

**Simple, straightforward, and it works!** 🎯
