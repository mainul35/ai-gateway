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

Settings live in [`config/config.properties`](config/config.properties) (`key=value`, `#` for comments).
The file is re-read when it changes, so no restart is needed. A value in the file takes precedence over
the matching environment variable; the variable is used when the property is empty or missing.

| Property | Environment variable | Default | Description |
|----------|----------------------|---------|-------------|
| `ollama.host` | `OLLAMA_HOST` | `http://localhost:11434` | Ollama server that models are deployed to, pulled into, listed from and deleted from |
| `hf.token` | `HF_TOKEN` | - | HuggingFace token, needed to read gated or private models (e.g. Llama) |
| `ollama.server.vram.gb` | `OLLAMA_SERVER_VRAM_GB` | detected | GPU memory of the Ollama server, for recommendations. Use `0` for a CPU-only server |
| `ollama.server.ram.gb` | `OLLAMA_SERVER_RAM_GB` | detected | RAM of the Ollama server, for recommendations |

Set the two hardware properties when the app does not run on the Ollama server itself (for example in
Docker, where only the container's resources are visible); otherwise the recommendations describe the
wrong machine.

These are environment variables only:

| Variable | Default | Description |
|----------|---------|-------------|
| `CONFIG_FILE` | `config/config.properties` | Path of the properties file |
| `HOST` | `0.0.0.0` | Address the web app binds to (`python app.py` only) |
| `PORT` | `5000` | Port the web app listens on (`python app.py` only) |
| `FLASK_DEBUG` | off | Set to `1` to enable Flask debug mode (never on an exposed server) |

## Docker

1. Set `ollama.host` (and ideally the server hardware) in `config/config.properties`.
   - Ollama on another machine: `http://<server-ip>:11434`
   - Ollama on the machine running Docker: `http://host.docker.internal:11434`

   The Ollama server must accept connections from other machines: start it with `OLLAMA_HOST=0.0.0.0`.

2. Build and start:
```bash
docker compose up -d --build
```

3. Open http://localhost:5000

The `config` folder is mounted into the container, so edits to `config/config.properties` apply to the
running container without a rebuild or restart. To run without Compose:
```bash
docker build -t ollama-model-checker .
docker run -d -p 5000:5000 -v "$(pwd)/config:/app/config:ro" --add-host host.docker.internal:host-gateway ollama-model-checker
```

The container serves the app with gunicorn using one worker process (required for cancelling deploys)
and multiple threads.

## Requirements

- Python 3.8+
- Ollama 0.5.5 or newer running on the same or an accessible server
- HuggingFace model ID (public GGUF models can be deployed directly)

## License

MIT

## Secrets

Credentials never go in tracked files, not even development defaults. Keep them in files git ignores:

- `.env` (copy `.env.example`): `GATEWAY_DB_PASSWORD`, which both compose files require, and
  `SEARXNG_SECRET` for the web-search container
- `config/config.properties` on the server: `gateway.database.url` (with that password),
  `gateway.master.key`, `gateway.session.secret`, `sso.client.secret`

There is deliberately no default database URL; the gateway refuses to start without one.

`scripts/git-hooks/pre-push` scans every commit being pushed and refuses the push if it finds something
that looks like a credential. Enable it once per clone:

```bash
git config core.hooksPath scripts/git-hooks
```

Run the same scan by hand with `python scripts/check_secrets.py --all`.

## Playground tools

The playground has three tools, each with an on/off toggle under the message box. The choice is
remembered per browser.

| Tool | What it does | Needs |
|---|---|---|
| **Web search** | The model writes a search query from the conversation. The gateway searches with SearXNG, reads the top pages, and the model answers with numbered citations that link to the sources. | the `searxng` compose service |
| **Image understanding** | Attach, paste or drop images for the model to look at. Models that can see images are marked in the model list, from Ollama's reported capabilities, an engine profile's `mmproj`, or `capabilities: [vision]` in `models.yaml`. | a vision model (e.g. `gemma4:31b`, `llava:13b`) |
| **Image generation** | Sending creates an image with Flux on ComfyUI instead of chatting. With an image attached, or after pressing **Edit this image**, it edits that image by instruction with Qwen-Image-Edit ("make it night", "remove the car"), keeping everything else. Up to two more pictures can be attached as references ("put the hat from image 2 on the person in image 1"). | ComfyUI with the Flux checkpoint and the Qwen-Image-Edit files below |

**The toggles say what is allowed, not what happens.** With more than one on, a small model
(`qwen3:1.7b`, about 0.2 s) reads each message and picks one action: a question goes to the web or
straight to the model, a description of a picture is drawn, and an instruction about an attached
picture edits it. The assistant's reply says which was chosen. To force one, leave only that toggle on.
Set `router.enabled=false` to go back to plain rules, or `router.model=` to another small model. The
same model also writes the search queries, so the big model is not loaded just for one line.

**A request is rewritten before it is drawn.** An image model reads a description of a picture, not a
message to an assistant: "Generate me an image of Java 27 based on the images available on the web"
drew an IDE screenshot, and the correction after it drew an unrelated landscape. The small model now
turns the request, the conversation and the pictures already made in it into one visual description,
which is shown as the image's caption (hover it to see what you asked). An edit is passed through as
written, since it already describes a change to a picture that exists. Turn it off with
`images.rewrite.prompt=false`.

Generating an image first unloads the language models, since Flux needs most of the 24 GB card, and
frees ComfyUI's VRAM afterwards. Images are stored in the database, and only their owner can open them.

The same image engine is available to API clients, such as Open WebUI and the OpenAI SDKs, at
`POST /v1/images/generations` and `POST /v1/images/edits` (multipart, with `strength`), which return
`b64_json`. Anyone with access to at least one model may generate images.

## How answers look

Answers are Markdown, so the playground renders them the way the
[MDViewer](https://github.com/mainul35/markdown-viewer) preview does: its palette, its headings, its
code plates, its tables. `app/static/markdown.css` is generated from MDViewer's own preview stylesheet
by `scripts/make_markdown_css.py`, scoped to `.md` so it styles answers and nothing else.

A half-finished answer is not valid Markdown, so streaming shows plain text and the finished answer is
rendered once, in one request to `/chat/render`. The gateway does the rendering: HTML the model wrote
is escaped rather than obeyed, links open in a new tab, and a picture from somewhere else becomes a
link instead of being fetched, so an answer cannot make the browser call out on its own.

Web answers are told today's date. Without it a model reads anything published after its training as
science fiction and refuses to answer: the same Java question came back saying the sources were "in
the future".

## Memory

A long chat eventually no longer fits in what a model can read, and a new chat starts from nothing.
The playground keeps two kinds of notes, both visible in its **Memory** panel:

- **This conversation** — a running summary of the older turns. Once a chat passes
  `memory.keep.recent` messages, those turns are replaced by the summary and only the recent ones are
  sent in full, so a long conversation keeps its thread instead of losing its beginning.
- **About you** — short facts that outlive one conversation, such as what you are building, your
  hardware, or how you like answers. They are added in front of every new chat.

Both are written by the conversation's own model, which is already loaded and has just read the
material, right after a reply and never while you wait. A note that contradicts an older one replaces
it, so changing your mind changes the memory.

**You stay in charge.** Click any note to correct it, press &times; to forget it, and **+ Note** to add
one yourself. Anything you write or edit is kept exactly as you left it and is never rewritten
automatically. Memory is per user: nobody else can read, edit or see yours.

```properties
memory.enabled=true
memory.model=                 # empty: each conversation's own model writes its notes
memory.summarize.every=8      # new messages before the summary is rewritten
memory.keep.recent=8          # recent turns always sent in full
memory.max.user.notes=20
```

Admins can switch a tool off for everyone in `config/config.properties`:

```properties
features.web_search.enabled=true
features.vision.enabled=true
features.image_generation.enabled=true
search.searxng.url=http://127.0.0.1:8888
images.comfyui.url=http://127.0.0.1:8188
images.checkpoint=flux1CompactCLIPAnd_Flux1DevFp16.safetensors
images.steps=20
images.edit.model=qwen_image_edit_2511_fp8mixed.safetensors
images.edit.text_encoder=qwen_2.5_vl_7b_fp8_scaled.safetensors
images.edit.vae=qwen_image_vae.safetensors
images.edit.lora=Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors
```

Image editing uses Qwen-Image-Edit-2511 (Apache-2.0) with the Lightning LoRA, which needs 4 steps
instead of 40. The files go in ComfyUI's `models/` folders:

| File | Folder | From |
|---|---|---|
| `qwen_image_edit_2511_fp8mixed.safetensors` (20.5 GB) | `diffusion_models` | Comfy-Org/Qwen-Image-Edit_ComfyUI |
| `qwen_2.5_vl_7b_fp8_scaled.safetensors` (9.4 GB) | `text_encoders` | Comfy-Org/Qwen-Image_ComfyUI |
| `qwen_image_vae.safetensors` | `vae` | Comfy-Org/Qwen-Image_ComfyUI |
| `Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors` | `loras` | lightx2v/Qwen-Image-Edit-2511-Lightning |

If they are missing, edits fall back to Flux image-to-image, which re-renders the picture towards the
prompt. A **Change** slider then sets how far it may move from the original.

Start the search container on the server with:

```bash
docker compose -f docker-compose.gateway.yml -f docker-compose.homelab.yml up -d searxng
```

## Deploying

From your machine:

```bash
bin/deploy.sh
```

It checks that the code compiles, backs up the server's current code (last 5 kept in
`.deploy-backup/`), syncs the project, reinstalls dependencies only when a requirements file
changed, restarts the gateway, and rolls back to the backup automatically if it does not come up
healthy. `config/config.properties` on the server is never overwritten, so secrets and SSO
settings survive every deploy.

Override the target with `DEPLOY_HOST`, `DEPLOY_DIR` and `DEPLOY_PUBLIC_URL`.

On the server the gateway is kept running by cron (`bin/gateway-start.sh` at boot and every
minute as a watchdog).
