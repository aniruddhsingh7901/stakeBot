#!/bin/bash
cd /root/stake
source venv/bin/activate

echo "Bittensor Stake Bot - Mode Comparison"
echo "======================================"
echo ""
echo "Choose mode to test:"
echo "  1) Block mode  - Fast test (~24 seconds per cycle)"
echo "  2) Epoch mode  - Full test (~72 minutes per cycle)"
echo "  3) Both modes  - Run block mode, then ask about epoch"
echo ""
read -p "Select (1-3): " choice

case $choice in
  1)
    export STAKE_MODE='block'
    echo ""
    echo "Testing BLOCK MODE:"
    echo "- Stakes on block N"
    echo "- Unstakes on block N+1"
    echo "- Takes ~24 seconds"
    ;;
  2)
    export STAKE_MODE='epoch'
    export EPOCHS_TO_STAKE='1'
    echo ""
    echo "Testing EPOCH MODE:"
    echo "- Stakes and holds for 360 blocks"
    echo "- Takes ~72 minutes"
    echo "- Earns emissions!"
    ;;
  3)
    export STAKE_MODE='block'
    echo ""
    echo "Starting with BLOCK MODE test first..."
    ;;
  *)
    echo "Invalid choice"
    exit 1
    ;;
esac

export WALLET_NAME='droplet'
export HOTKEY_NAME='default'
export VALIDATOR_HOTKEY='5D7aRtpmVBKsQRzMA2ioUPL25onJPzBjiFVVt5uPZ3TDsn51'
export STAKE_AMOUNT='0.05'
export NETUID='51'
export NETWORK='finney'
export CONTINUOUS='false'
export WALLET_PASSWORD=''

echo ""
read -p "Press Enter to start..."
python3 stake_bot.py

if [ "$choice" == "3" ]; then
  echo ""
  echo "Block mode test complete!"
  read -p "Do you want to test Epoch mode now? (y/n): " test_epoch
  if [ "$test_epoch" == "y" ]; then
    export STAKE_MODE='epoch'
    export EPOCHS_TO_STAKE='1'
    echo ""
    echo "Starting EPOCH MODE test (will take ~72 minutes)..."
    read -p "Press Enter to continue..."
    python3 stake_bot.py
  fi
fi
