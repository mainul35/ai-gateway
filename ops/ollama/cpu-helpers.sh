#!/usr/bin/env bash
# The gateway's two small helper models, as CPU-only copies that never take GPU memory:
#
#   qwen3-1.7b-cpu             router.model: names the tool and the kind of work for every playground turn
#   qwen3-embedding-0.6b-cpu   knowledge.embedding.model: embeds knowledge-base passages and queries
#
# Same weights as qwen3:1.7b and qwen3-embedding:0.6b (so the knowledge index stays valid), with
# num_gpu 0 and an 8K window. On the 5950X the router answers at about 44 tokens/s and an embedding
# takes about 2 s; together they hold about 4.4 GB of RAM and nothing on the GPU, so a big model never
# has to make room for them. No root needed: it talks to Ollama's API. Safe to run again.
set -euo pipefail
OLLAMA=${OLLAMA_HOST:-http://127.0.0.1:11434}

make() {   # base model, CPU copy
    ollama pull "$1" >/dev/null
    curl -fsS "$OLLAMA/api/create" -d "{\"model\":\"$2\",\"from\":\"$1\",\"parameters\":{\"num_gpu\":0,\"num_ctx\":8192},\"stream\":false}"
    echo "  <- $2"
}
make qwen3:1.7b qwen3-1.7b-cpu
make qwen3-embedding:0.6b qwen3-embedding-0.6b-cpu
echo "Then in config/config.properties:"
echo "  router.model=qwen3-1.7b-cpu"
echo "  knowledge.embedding.model=qwen3-embedding-0.6b-cpu"
