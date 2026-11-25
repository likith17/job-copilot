#!/usr/bin/env bash
# One-time setup of the training environment in WSL/Linux (no sudo needed).
# Creates ~/jc-train: Python 3.11, CUDA PyTorch, Unsloth. About 6 GB of downloads.
set -euo pipefail
if ! command -v uv >/dev/null && [ ! -x "$HOME/.local/bin/uv" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
"$UV" venv --python 3.11 "$HOME/jc-train"
"$UV" pip install --python "$HOME/jc-train/bin/python" unsloth
"$HOME/jc-train/bin/python" -c "import torch; print('torch', torch.__version__, 'cuda', torch.cuda.is_available())"
echo "Ready: ~/jc-train/bin/python training/train_qlora.py"
