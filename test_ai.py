"""Quick standalone test of the pip-only local AI. Run:

    python test_ai.py

First ever run downloads the model once (~4.7 GB, with a progress bar);
after that it loads from disk in a minute or two. If this script prints
an answer at the end, ask_library.py will work in full AI mode.
"""
from llama_cpp import Llama

print("Loading the model (first run downloads ~4.7 GB — watch for a progress bar)...")
llm = Llama.from_pretrained(
    repo_id="Qwen/Qwen2.5-7B-Instruct-GGUF",
    filename="*q4_k_m.gguf",
    n_ctx=8192,
    verbose=False,
)
print("Model loaded. Asking a test question (this takes a moment on CPU)...")

out = llm.create_chat_completion(
    messages=[{"role": "user", "content": "In one sentence, what limits the stability of tin-based perovskite solar cells?"}],
    max_tokens=80,
    temperature=0.2,
)
print()
print("ANSWER:", out["choices"][0]["message"]["content"].strip())
print()
print("The local AI works — ask_library.py will now use it automatically.")
