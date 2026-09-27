@echo off
cd /d C:\Users\sharv\Projects\strategy-bot\backend
set PYTHONPATH=C:\Users\sharv\Projects\strategy-bot\backend
python tests\_parity_probe.py > parity_out.txt 2>&1
echo DONE >> parity_out.txt
