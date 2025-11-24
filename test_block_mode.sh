#!/bin/bash
# Quick test of block mode
cd /root/stake
source venv/bin/activate 2>/dev/null || true

echo "Starting Block Mode Test..."
echo ""
echo "This will:"
echo "1. Stake 0.05 TAO"
echo "2. Wait for 1 block (~12 seconds)"
echo "3. Unstake immediately"
echo ""
echo "Press Ctrl+C to cancel, or Enter to continue..."
read

python3 stake_bot.py
