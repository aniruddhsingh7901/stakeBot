#!/bin/bash
cd /root/stake
source venv/bin/activate

export WALLET_NAME='droplet'
export HOTKEY_NAME='default'
export VALIDATOR_HOTKEY='5D7aRtpmVBKsQRzMA2ioUPL25onJPzBjiFVVt5uPZ3TDsn51'
export STAKE_AMOUNT='0.05'
export NETUID='51'
export NETWORK='finney'
export STAKE_MODE='block'
export CONTINUOUS='false'
export WALLET_PASSWORD=''

echo "Testing Block Mode (single cycle)..."
echo "=================================="
python3 stake_bot.py
