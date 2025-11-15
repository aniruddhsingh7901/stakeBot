// ===============================================
// PM2 CONFIGURATION FOR BITTENSOR STAKE BOT
// ===============================================
//
// ⚠️  IMPORTANT: YOU MUST EDIT THIS FILE BEFORE RUNNING!
//
// Required changes:
//   1. Set VALIDATOR_HOTKEY (your validator's SS58 address)
//   2. Set WALLET_PASSWORD (your wallet password)
//
// Optional changes:
//   - WALLET_NAME: Your wallet name (default: 'default')
//   - HOTKEY_NAME: Your hotkey name (default: 'default')
//   - STAKE_AMOUNT: Amount to stake in TAO (default: 0.001)
//   - NETUID: Subnet ID (default: 1)
//   - NETWORK: 'test' or 'finney' (default: 'test')
//   - CONTINUOUS: 'true' or 'false' (default: 'true')
//
// After editing, run: pm2 start ecosystem.config.js
//
// ===============================================

module.exports = {
  apps: [{
    name: 'stake-bot',
    script: 'stake_bot.py',
    interpreter: 'python3',
    
    // ⚠️ EDIT THE VALUES BELOW ⚠️
    env: {
      WALLET_NAME: 'default',                    // Your wallet name
      HOTKEY_NAME: 'default',                    // Your hotkey name
      VALIDATOR_HOTKEY: '',                      // ⚠️ REQUIRED: Set validator SS58 address
      STAKE_AMOUNT: '0.001',                     // Amount to stake (TAO)
      NETUID: '1',                               // Subnet ID
      NETWORK: 'test',                           // 'test' or 'finney'
      CONTINUOUS: 'true',                        // 'true' for continuous operation
      WALLET_PASSWORD: '',                       // ⚠️ REQUIRED: Your wallet password
      
      // Alternative: Use keyfile instead of password (more secure)
      // Leave WALLET_PASSWORD empty and ensure your keyfile is properly set up
    },
    
    // PM2 settings
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: '1G',
    
    // Logging
    error_file: './logs/stake-bot-error.log',
    out_file: './logs/stake-bot-out.log',
    log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
    
    // Restart settings
    restart_delay: 4000,
    min_uptime: '10s',
    max_restarts: 10,
  }]
};

