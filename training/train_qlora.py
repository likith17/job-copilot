"""Fine-tune your own resume-writing model: QLoRA on Qwen3-8B with Unsloth.

Runs on an 8 GB GPU (tested target: RTX 4070 Laptop) inside WSL/Linux:

    bash training/setup_wsl.sh                       # once: creates ~/jc-train with CUDA PyTorch + Unsloth
    ~/jc-train/bin/python training/train_qlora.py    # trains on data/training/sft/, ~1-2 h for ~3k examples
    ollama create jc-writer -f data/training/model/Modelfile --quantize q4_K_M     (Windows side)
    python -m copilot eval --model jc-writer --finetuned                            measure it

Input: data/training/sft/{train,val}.jsonl from `python -m copilot train-data --build`.
Output: data/training/model/ (merged weights + Modelfile for Ollama).
"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="unsloth/Qwen3-8B-unsloth-bnb-4bit")
ap.add_argument("--epochs", type=float, default=2)
ap.add_argument("--lr", type=float, default=2e-4)
ap.add_argument("--rank", type=int, default=16)
ap.add_argument("--max-len", type=int, default=2048)
ap.add_argument("--data", default=str(ROOT / "data" / "training" / "sft"))
ap.add_argument("--out", default=str(ROOT / "data" / "training" / "model"))
args = ap.parse_args()

from unsloth import FastLanguageModel                                   # noqa: E402  (import order matters)
from unsloth.chat_templates import train_on_responses_only               # noqa: E402
from datasets import load_dataset                                        # noqa: E402
from trl import SFTConfig, SFTTrainer                                    # noqa: E402

model, tokenizer = FastLanguageModel.from_pretrained(args.base, max_seq_length=args.max_len, load_in_4bit=True)
model = FastLanguageModel.get_peft_model(
    model, r=args.rank, lora_alpha=args.rank, lora_dropout=0,
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    use_gradient_checkpointing="unsloth", random_state=3407)

files = {"train": f"{args.data}/train.jsonl"}
if Path(f"{args.data}/val.jsonl").stat().st_size:
    files["val"] = f"{args.data}/val.jsonl"
ds = load_dataset("json", data_files=files)


def to_text(ex):
    # No thinking: the task is short and structured, and thinking would slow every bullet down.
    return {"text": tokenizer.apply_chat_template(ex["messages"], tokenize=False, enable_thinking=False)}


ds = ds.map(to_text, remove_columns=[c for c in ds["train"].column_names if c != "text"])
too_long = sum(len(tokenizer(t)["input_ids"]) > args.max_len for t in ds["train"]["text"])
print(f"train {len(ds['train'])} examples ({too_long} longer than {args.max_len} tokens, truncated)")

trainer = SFTTrainer(
    model=model, tokenizer=tokenizer, train_dataset=ds["train"], eval_dataset=ds.get("val"),
    args=SFTConfig(
        dataset_text_field="text", max_seq_length=args.max_len, packing=False,
        per_device_train_batch_size=2, gradient_accumulation_steps=8,
        num_train_epochs=args.epochs, learning_rate=args.lr, lr_scheduler_type="cosine", warmup_ratio=0.05,
        optim="adamw_8bit", weight_decay=0.01, logging_steps=10,
        eval_strategy="epoch" if "val" in ds else "no", save_strategy="no",
        output_dir=f"{args.out}/checkpoints", report_to="none", seed=3407))
# Learn only the answers, not to reproduce the prompts.
trainer = train_on_responses_only(trainer, instruction_part="<|im_start|>user\n",
                                  response_part="<|im_start|>assistant\n")
stats = trainer.train()

out = Path(args.out)
out.mkdir(parents=True, exist_ok=True)
model.save_pretrained(str(out / "lora"))
tokenizer.save_pretrained(str(out / "lora"))
model.save_pretrained_merged(str(out / "merged"), tokenizer, save_method="merged_16bit")
(out / "Modelfile").write_text(
    "FROM ./merged\n"
    "PARAMETER temperature 0.3\n"
    "PARAMETER num_ctx 4096\n"
    'PARAMETER stop "<|im_end|>"\n', encoding="utf-8")
(out / "train_stats.json").write_text(json.dumps({**stats.metrics, **vars(args)}, indent=1), encoding="utf-8")
print(f"\nDone. Now, on Windows:\n  cd {out}\n  ollama create jc-writer -f Modelfile --quantize q4_K_M\n"
      "  python -m copilot eval --model jc-writer --finetuned")
