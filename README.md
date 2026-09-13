# Ollama Model Checker

A web application to check HuggingFace model compatibility with your AI server and deploy them to Ollama.

## Features

- **System Resource Monitoring**: Check your GPU VRAM, RAM, and CPU status
- **Model Compatibility Analysis**: Enter any HuggingFace model ID to check compatibility
- **Quantization Recommendations**: Get intelligent suggestions for optimal quantization levels
- **KV Cache Calculator**: Calculate and adjust KV cache requirements
- **One-Click Deployment**: Deploy models to Ollama with a single click
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
2. **Enter Model ID**: Input a HuggingFace model ID (e.g., `meta-llama/Llama-2-7b-chat-hf`)
3. **Check Compatibility**: Click "Check Compatibility" to analyze the model
4. **Select Quantization**: Choose from recommended quantization levels
5. **Adjust Context Length**: Use the slider to set your desired context length
6. **Deploy**: Click "Deploy Model" to create the model in Ollama

## Quantization Levels

- **Q2_K**: Maximum compression, lower quality (2-bit)
- **Q3_K_M**: Good compression with decent quality (3-bit)
- **Q4_0/Q4_1**: Good balance of quality and performance (4-bit)
- **Q5_0/Q5_1**: Better quality with moderate memory (5-bit)
- **Q6_K**: High quality with good compression (6-bit)
- **Q8_0**: Near-original quality (8-bit)

## API Endpoints

- `GET /api/system-info` - Get system resource information
- `GET /api/check-ollama` - Check Ollama status
- `POST /api/check-model` - Check HuggingFace model compatibility
- `POST /api/deploy` - Deploy model to Ollama
- `POST /api/pull-model` - Pull model from Ollama
- `GET /api/list-models` - List existing models
- `POST /api/delete-model` - Delete a model

## Configuration

Set the `OLLAMA_HOST` environment variable if Ollama is running on a different host:
```bash
export OLLAMA_HOST=http://your-server:11434
```

## Requirements

- Python 3.8+
- Ollama running on the same or accessible server
- HuggingFace model ID (public models work best)

## License

MIT
