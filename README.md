# Ollama Model Checker

A web application to check HuggingFace model compatibility with your AI server and deploy them to Ollama.

## Features

- **System Resource Monitoring**: Check your GPU VRAM, RAM, and CPU status
- **Model Compatibility Analysis**: Enter any HuggingFace model ID to check compatibility
- **Quantization Recommendations**: Get suggestions for the best quantization level that fits your GPU (or GPU + CPU / CPU only)
- **KV Cache Calculator**: Calculate KV cache requirements (GQA-aware) and adjust the context length
- **One-Click Deployment**: Deploy GGUF models from HuggingFace to Ollama with a single click
- **Model Management**: View, pull, and delete models from your Ollama instance

## Installation

1. Install Python dependencies:
```bash
pip install -r requirements.txt
```

2. Make sure Ollama is running on your server (default: http://localhost:11434)

3. Start the application:
```bash
python app.py
```

4. Open your browser and navigate to: http://localhost:5000

## Usage

1. **Check System Status**: The app automatically detects your GPU and system resources
2. **Enter Model ID**: Input a HuggingFace model ID (e.g., `bartowski/Llama-3.2-1B-Instruct-GGUF`)
3. **Check Compatibility**: Click "Check Compatibility" to analyze the model
4. **Select Quantization**: Choose from recommended quantization levels
5. **Adjust Context Length**: Use the slider to set your desired context length
6. **Deploy**: Click "Deploy Model" to create the model in Ollama

### Which models can be deployed?

Ollama can only pull models from HuggingFace that are published as **GGUF** files
(`hf.co/owner/model:QUANT`). For regular repos (safetensors, e.g. `meta-llama/Llama-2-7b-chat-hf`)
the app still shows size, memory, and KV cache estimates, but deployment is disabled. Search
HuggingFace for a GGUF version of the model (e.g. `bartowski/...-GGUF`) to deploy it.

- **Deploy Model** creates `<model>:<quant>` with your chosen context length (`num_ctx`).
- **Pull into Ollama** downloads `hf.co/owner/model:QUANT` with default settings.

## Quantization Levels

- **Q2_K**: Maximum compression, lower quality (2-bit)
- **Q3_K_M**: Good compression with decent quality (3-bit)
- **Q4_0/Q4_1**: Good balance of quality and performance (4-bit)
- **Q4_K_M**: Most popular choice, best 4-bit quality (4-bit)
- **Q5_0/Q5_1/Q5_K_M**: Better quality with moderate memory (5-bit)
- **Q6_K**: High quality with good compression (6-bit)
- **Q8_0**: Near-original quality (8-bit)

## API Endpoints

- `GET /api/system-info` - Get system resource information
- `GET /api/check-ollama` - Check Ollama status
- `POST /api/check-model` - Check HuggingFace model compatibility
- `POST /api/deploy` - Deploy model to Ollama
- `POST /api/pull-model` - Pull a GGUF model from HuggingFace into Ollama
- `POST /api/cancel` - Cancel a running deploy or pull (`{"operation_id": "..."}`)

`/api/deploy` and `/api/pull-model` accept `"stream": true` to receive live progress as
newline-delimited JSON, and an optional `"operation_id"` that can be passed to `/api/cancel`.
- `GET /api/list-models` - List existing models
- `POST /api/delete-model` - Delete a model

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server address (`0.0.0.0` / missing scheme or port are handled) |
| `HF_TOKEN` | - | HuggingFace token, needed to read gated or private models (e.g. Llama) |
| `HOST` | `0.0.0.0` | Address the web app binds to |
| `PORT` | `5000` | Port the web app listens on |
| `FLASK_DEBUG` | off | Set to `1` to enable Flask debug mode (never on an exposed server) |

```bash
export OLLAMA_HOST=http://your-server:11434
```

## Requirements

- Python 3.8+
- Ollama 0.5.5 or newer running on the same or an accessible server
- HuggingFace model ID (public GGUF models can be deployed directly)

## License

MIT
