document.addEventListener('DOMContentLoaded', function() {
    const $ = id => document.getElementById(id);

    const modelInput = $('modelInput');
    const checkBtn = $('checkBtn');
    const resultsSection = $('resultsSection');
    const modelNotice = $('modelNotice');
    const recommendationsGrid = $('recommendationsGrid');
    const kvCacheInfo = $('kvCacheInfo');
    const contextLength = $('contextLength');
    const contextValue = $('contextValue');
    const deployBtn = $('deployBtn');
    const pullBtn = $('pullBtn');
    const quickDeployBtn = $('quickDeployBtn');
    const deployStatus = $('deployStatus');
    const modelsList = $('modelsList');

    const state = {
        data: null,
        selected: null,   // quantization chosen by the user
        best: null,       // best deployable quantization, used by Quick Deploy
        busy: false,
        operation: null,  // running deploy/pull: { id, controller, cancelling }
    };

    checkOllamaStatus();
    loadSystemInfo();
    loadExistingModels();

    checkBtn.addEventListener('click', checkModel);
    modelInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') checkModel();
    });

    contextLength.addEventListener('input', function() {
        contextValue.textContent = this.value;
        updateKvAtContext();
        updateDeployInfo();
    });

    recommendationsGrid.addEventListener('click', function(e) {
        const card = e.target.closest('.quant-card');
        if (card) selectQuantization(card.dataset.quantization);
    });

    modelsList.addEventListener('click', function(e) {
        const button = e.target.closest('[data-delete-model]');
        if (button) deleteModel(button.dataset.deleteModel);
    });

    deployBtn.addEventListener('click', () => deployModel(state.selected, 'Deploying'));
    quickDeployBtn.addEventListener('click', () => deployModel(state.best, 'Quick deploying'));
    pullBtn.addEventListener('click', pullModel);
    if ($('cancelDeployBtn')) $('cancelDeployBtn').addEventListener('click', cancelOperation);

    function checkOllamaStatus() {
        const dot = $('ollamaStatusDot');
        const status = $('ollamaStatus');

        fetch('/api/check-ollama')
            .then(res => res.json())
            .then(data => {
                dot.classList.remove('online', 'offline');
                if (data.status === 'running') {
                    dot.classList.add('online');
                    status.textContent = `Ollama Running (v${data.version})`;
                } else if (data.status === 'offline') {
                    dot.classList.add('offline');
                    status.textContent = `Ollama Offline (${data.host})`;
                } else {
                    dot.classList.add('offline');
                    status.textContent = `Ollama Error: ${data.message || 'unknown'}`;
                }
            })
            .catch(() => {
                dot.classList.add('offline');
                status.textContent = 'Ollama Offline';
            });
    }

    function loadSystemInfo() {
        fetch('/api/system-info')
            .then(res => res.json())
            .then(info => {
                if (info.gpu_source === 'config') {
                    $('gpuInfo').textContent = info.total_vram
                        ? `Ollama server GPU: ${formatBytes(info.total_vram)} VRAM (configured)`
                        : 'Ollama server: no GPU (configured)';
                } else {
                    $('gpuInfo').textContent = info.gpu_info.length
                        ? info.gpu_info.map(gpu => `${gpu.name} (${formatBytes(gpu.vram_free)} free / ${formatBytes(gpu.vram_total)})`).join(', ')
                        : 'No GPU detected (CPU only)';
                }
                $('ramInfo').textContent = info.ram_source === 'config'
                    ? `Ollama server RAM: ${formatBytes(info.total_ram)} (configured)`
                    : `RAM: ${formatBytes(info.available_ram)} free / ${formatBytes(info.total_ram)}`;
            })
            .catch(() => {
                $('gpuInfo').textContent = 'GPU info unavailable';
                $('ramInfo').textContent = 'RAM info unavailable';
            });
    }

    function loadExistingModels() {
        fetch('/api/list-models')
            .then(res => res.json())
            .then(data => {
                if (data.error) {
                    modelsList.innerHTML = `<p class="empty-state">Unable to load models: ${escapeHtml(data.error)}</p>`;
                    return;
                }

                if (!data.length) {
                    modelsList.innerHTML = '<p class="empty-state">No models installed</p>';
                    return;
                }

                modelsList.innerHTML = data.map(model => `
                    <div class="model-item">
                        <div>
                            <div class="model-name">${escapeHtml(model.name)}</div>
                            <div class="model-size">${formatBytes(model.size)}</div>
                        </div>
                        <button class="btn btn-danger" data-delete-model="${escapeHtml(model.name)}">Delete</button>
                    </div>
                `).join('');
            })
            .catch(() => {
                modelsList.innerHTML = '<p class="empty-state">Unable to load models</p>';
            });
    }

    async function checkModel() {
        const modelId = modelInput.value.trim();
        if (!modelId) {
            alert('Please enter a HuggingFace model ID');
            return;
        }

        checkBtn.disabled = true;
        checkBtn.textContent = 'Checking...';

        try {
            const response = await fetch('/api/check-model', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ model_id: modelId })
            });

            const data = await response.json();

            if (!response.ok || data.error) {
                alert(`Error: ${data.error || response.statusText}`);
                return;
            }

            const deployable = data.recommendations.filter(rec => rec.available);
            state.data = data;
            state.best = (deployable.find(rec => rec.recommended) || deployable[0] || {}).quantization || null;
            state.selected = state.best || (data.recommendations[0] || {}).quantization || null;

            setStatus('', '');
            displayModelInfo(data);
            displayNotice(data);
            renderRecommendations();
            displayKvCache(data.kv_cache);
            updateDeployInfo();
            resultsSection.style.display = 'block';
        } catch (err) {
            alert('Failed to check model: ' + err.message);
        } finally {
            checkBtn.disabled = false;
            checkBtn.textContent = 'Check Compatibility';
        }
    }

    function displayModelInfo(data) {
        const info = data.model_info;
        const arch = data.model_architecture;

        $('modelId').textContent = info.id;
        $('modelAuthor').textContent = info.author || 'N/A';
        $('modelPipeline').textContent = info.pipeline_tag || 'N/A';
        $('modelLikes').textContent = (info.likes || 0).toLocaleString();
        $('modelDownloads').textContent = (info.downloads || 0).toLocaleString();

        $('modelType').textContent = arch.model_type || 'N/A';
        $('modelArchitectures').textContent = (arch.architectures || []).join(', ') || 'N/A';
        $('modelHiddenSize').textContent = arch.hidden_size || 'N/A';
        $('modelLayers').textContent = arch.num_hidden_layers || 'N/A';
        $('modelHeads').textContent = arch.num_attention_heads
            ? `${arch.num_attention_heads}${arch.num_key_value_heads ? ` (${arch.num_key_value_heads} KV)` : ''}`
            : 'N/A';
        $('modelVocab').textContent = arch.vocab_size ? arch.vocab_size.toLocaleString() : 'N/A';
        $('modelMaxPos').textContent = arch.max_position_embeddings ? arch.max_position_embeddings.toLocaleString() : 'N/A';

        $('modelParams').textContent = data.parameters.count
            ? `${formatParams(data.parameters.count)} (${data.parameters.source})`
            : 'Unknown';
        $('modelTotalSize').textContent = formatBytes(data.model_sizes.total_size);
        $('modelFileCount').textContent = data.model_sizes.file_count;
        $('modelFormat').textContent = data.is_gguf_repo ? 'GGUF (deployable)' : 'Not GGUF';
    }

    function displayNotice(data) {
        const notices = [];
        if (!data.is_gguf_repo) {
            notices.push('This repository has no GGUF files in a supported quantization, so Ollama cannot pull it directly. ' +
                'The sizes below are estimates. To deploy, search HuggingFace for a GGUF version of this model and check that instead.');
        }
        if (data.kv_cache.estimated) {
            notices.push('The model config could not be read (the repo may be gated; set HF_TOKEN to access it). ' +
                'KV cache figures use default values.');
        }
        modelNotice.innerHTML = notices.map(text => `<p>${escapeHtml(text)}</p>`).join('');
        modelNotice.hidden = notices.length === 0;
    }

    function renderRecommendations() {
        const recommendations = state.data.recommendations;
        if (!recommendations.length) {
            recommendationsGrid.innerHTML = state.data.parameters.count || state.data.is_gguf_repo
                ? '<p class="empty-state">No quantization fits in your available memory</p>'
                : '<p class="empty-state">Model size is unknown, so no recommendation can be made</p>';
            return;
        }

        recommendationsGrid.innerHTML = recommendations.map(rec => {
            const qualityClass = rec.quality === 'High' || rec.quality === 'Very High' ? 'quality-high' :
                               rec.quality === 'Medium' ? 'quality-medium' : 'quality-low';
            const isSelected = state.selected === rec.quantization;
            const classes = ['quant-card', rec.recommended ? 'recommended' : '', isSelected ? 'selected' : ''].join(' ');

            return `
                <div class="${classes}" data-quantization="${escapeHtml(rec.quantization)}">
                    <div class="quant-header">
                        <span class="quant-name">${escapeHtml(rec.name)}</span>
                        <span class="quant-bits">${rec.bits}-bit</span>
                    </div>
                    <span class="quant-quality ${qualityClass}">${escapeHtml(rec.quality)}</span>
                    <p class="quant-description">${escapeHtml(rec.description)}</p>
                    <div class="quant-stats">
                        <div class="quant-stat">
                            <span class="stat-label">Size:</span>
                            <span class="stat-value">${rec.size_is_exact ? '' : '~'}${rec.estimated_size_gb} GB</span>
                        </div>
                        <div class="quant-stat">
                            <span class="stat-label">Memory:</span>
                            <span class="stat-value">${rec.memory_needed_gb} GB</span>
                        </div>
                        <div class="quant-stat">
                            <span class="stat-label">Runs on:</span>
                            <span class="stat-value">${rec.run_mode}</span>
                        </div>
                        <div class="quant-stat">
                            <span class="stat-label">Speed:</span>
                            <span class="stat-value">${escapeHtml(rec.speed)}</span>
                        </div>
                    </div>
                    ${isSelected ? '<div class="selected-badge">✓ Selected</div>' : ''}
                </div>
            `;
        }).join('');
    }

    function displayKvCache(kvCache) {
        kvCacheInfo.innerHTML = `
            <div class="kv-cache-grid">
                <div class="kv-stat">
                    <div class="kv-stat-value">${formatBytes(kvCache.kv_cache_per_token_bytes)}</div>
                    <div class="kv-stat-label">Per Token</div>
                </div>
                <div class="kv-stat">
                    <div class="kv-stat-value" id="kvAtContext"></div>
                    <div class="kv-stat-label">At Selected Context</div>
                </div>
                <div class="kv-stat">
                    <div class="kv-stat-value">${kvCache.kv_cache_total_gb} GB</div>
                    <div class="kv-stat-label">At Max Context</div>
                </div>
                <div class="kv-stat">
                    <div class="kv-stat-value">${kvCache.max_context_length.toLocaleString()}</div>
                    <div class="kv-stat-label">Max Context</div>
                </div>
            </div>
        `;

        // Set max before value so the browser doesn't clamp the value to the previous max
        contextLength.max = Math.max(kvCache.max_context_length, Number(contextLength.min));
        contextLength.value = kvCache.recommended_context_length;
        contextValue.textContent = contextLength.value;
        updateKvAtContext();
    }

    function updateKvAtContext() {
        const target = $('kvAtContext');
        if (target && state.data) {
            target.textContent = formatBytes(state.data.kv_cache.kv_cache_per_token_bytes * Number(contextLength.value));
        }
    }

    function findRecommendation(quantization) {
        return state.data ? state.data.recommendations.find(rec => rec.quantization === quantization) : null;
    }

    function selectQuantization(quantization) {
        state.selected = quantization;
        renderRecommendations();
        updateDeployInfo();
    }

    function updateDeployInfo() {
        const selected = findRecommendation(state.selected);
        const best = findRecommendation(state.best);

        $('deployModelName').textContent = state.data ? state.data.model_info.id : '-';
        $('deployQuantName').textContent = selected ? selected.name : '-';
        $('deployContextLength').textContent = contextLength.value;
        $('quickDeployModel').textContent = best
            ? `${state.data.model_info.id} (${best.name})`
            : 'No deployable GGUF quantization found';

        const canDeploySelected = !state.busy && Boolean(selected && selected.available);
        deployBtn.disabled = !canDeploySelected;
        pullBtn.disabled = !canDeploySelected;
        quickDeployBtn.disabled = state.busy || !best;
    }

    function deployModel(quantization, verb) {
        const rec = findRecommendation(quantization);
        if (!rec || !rec.available) {
            alert('Please check a GGUF model and select an available quantization first');
            return;
        }

        runOllamaAction(
            `${verb} ${state.data.model_info.id} (${rec.name})`,
            '/api/deploy',
            { model_id: state.data.model_info.id, quantization: rec.quantization, context_length: Number(contextLength.value) },
            'deployed',
            'Deployment'
        );
    }

    function pullModel() {
        const rec = findRecommendation(state.selected);
        if (!rec || !rec.available) {
            alert('Please check a GGUF model and select an available quantization first');
            return;
        }

        runOllamaAction(
            `Pulling ${state.data.model_info.id} (${rec.name}) into Ollama`,
            '/api/pull-model',
            { model_id: state.data.model_info.id, quantization: rec.quantization },
            'pulled',
            'Pull'
        );
    }

    async function runOllamaAction(title, url, body, successVerb, failureLabel) {
        const operation = { id: newOperationId(), controller: new AbortController(), cancelling: false };
        const cancelledMessage = `${failureLabel} cancelled. Ollama keeps the partial download, so trying again resumes it.`;
        state.operation = operation;
        state.busy = true;
        updateDeployInfo();
        setStatus('', '');
        resetCancelButton();
        let progress = null;
        let result = null;

        try {
            progress = createProgressTracker(title);
            const response = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ...body, stream: true, operation_id: operation.id }),
                signal: operation.controller.signal
            });

            // Validation errors come back as plain JSON; progress comes back as NDJSON
            if ((response.headers.get('Content-Type') || '').includes('application/x-ndjson')) {
                await readNdjson(response, event => {
                    if (event.done) result = event;
                    else progress.update(event);
                });
            } else {
                result = await response.json();
            }

            if (result && result.success) {
                setStatus('success', `✓ Model ${successVerb} successfully as ${result.model_name}`);
                loadExistingModels();
            } else if (operation.cancelling || (result && result.cancelled)) {
                setStatus('cancelled', cancelledMessage);
            } else {
                setStatus('error', `✗ ${failureLabel} failed: ${result ? result.error : 'the server closed the connection before finishing'}`);
            }
        } catch (err) {
            if (err.name === 'AbortError' || operation.cancelling) {
                setStatus('cancelled', cancelledMessage);
            } else {
                setStatus('error', `✗ Error: ${err.message}`);
            }
        } finally {
            if (progress) progress.stop();
            state.operation = null;
            state.busy = false;
            updateDeployInfo();
        }
    }

    function newOperationId() {
        // crypto.randomUUID is unavailable on plain-HTTP LAN addresses, so build the ID by hand
        return Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
    }

    function resetCancelButton() {
        const button = $('cancelDeployBtn');
        if (button) {
            button.disabled = false;
            button.textContent = 'Cancel';
        }
    }

    async function cancelOperation() {
        const operation = state.operation;
        if (!operation || operation.cancelling) return;
        operation.cancelling = true;

        const button = $('cancelDeployBtn');
        if (button) {
            button.disabled = true;
            button.textContent = 'Cancelling...';
        }

        // Fallback: dropping the connection also stops the download, since Ollama aborts when its client disconnects
        setTimeout(() => operation.controller.abort(), 5000);
        try {
            const response = await fetch('/api/cancel', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ operation_id: operation.id })
            });
            if (!response.ok) operation.controller.abort();
        } catch (err) {
            operation.controller.abort();
        }
    }

    async function readNdjson(response, onEvent) {
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop();  // keep the incomplete last line for the next chunk
            lines.filter(line => line.trim()).forEach(line => onEvent(JSON.parse(line)));
        }

        buffer += decoder.decode();
        if (buffer.trim()) onEvent(JSON.parse(buffer));
    }

    function createProgressTracker(title) {
        const layers = new Map();  // digest -> { total, completed, startCompleted }
        const samples = [];        // [timestamp ms, bytes downloaded] over the last few seconds
        const fill = $('progressFill');

        if (!$('deployProgress') || !fill) {
            // The page HTML is older than this script (server not restarted); deploy without the progress panel
            console.warn('Progress panel not found in page; reload the page after restarting the server.');
            return { update() {}, stop() {} };
        }

        $('progressTitle').textContent = title;
        $('progressStep').textContent = 'Starting...';
        $('progressPercent').textContent = '';
        $('progressSize').textContent = '';
        $('progressSpeed').textContent = '';
        $('progressEta').textContent = '';
        fill.style.width = '';
        fill.classList.add('indeterminate');
        $('deployProgress').hidden = false;

        function renderDownload() {
            let total = 0, completed = 0, downloaded = 0;
            for (const layer of layers.values()) {
                total += layer.total;
                completed += layer.completed;
                // Layers that already existed locally report as complete immediately; don't count them as speed
                downloaded += layer.completed - layer.startCompleted;
            }

            const now = performance.now();
            samples.push([now, downloaded]);
            while (samples.length > 2 && now - samples[0][0] > 5000) samples.shift();
            const elapsedSeconds = (now - samples[0][0]) / 1000;
            const speed = elapsedSeconds > 0.5 ? (downloaded - samples[0][1]) / elapsedSeconds : 0;
            const percent = total ? Math.min(completed / total * 100, 100) : 0;

            fill.classList.remove('indeterminate');
            fill.style.width = `${percent.toFixed(1)}%`;
            $('progressPercent').textContent = `${percent.toFixed(1)}%`;
            $('progressSize').textContent = `${formatBytes(completed)} / ${formatBytes(total)}`;
            $('progressSpeed').textContent = speed > 0 ? `${formatBytes(speed)}/s` : '';
            $('progressEta').textContent = speed > 0 && completed < total
                ? `${formatDuration((total - completed) / speed)} remaining`
                : '';
        }

        return {
            update(event) {
                if (event.status) $('progressStep').textContent = event.status;
                if (event.digest && event.total) {
                    const layer = layers.get(event.digest) || { startCompleted: event.completed || 0 };
                    layer.total = event.total;
                    layer.completed = event.completed || 0;
                    layers.set(event.digest, layer);
                    renderDownload();
                }
            },
            stop() {
                $('deployProgress').hidden = true;
            }
        };
    }

    async function deleteModel(modelName) {
        if (!confirm(`Delete ${modelName}?`)) return;

        try {
            const response = await fetch('/api/delete-model', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ model_name: modelName })
            });

            const data = await response.json();

            if (data.success) {
                loadExistingModels();
            } else {
                alert(`Failed to delete: ${data.error}`);
            }
        } catch (err) {
            alert(`Error: ${err.message}`);
        }
    }

    function setStatus(type, text) {
        deployStatus.className = type ? `deploy-status ${type}` : 'deploy-status';
        deployStatus.textContent = text;
    }

    function formatBytes(bytes) {
        if (!bytes || bytes < 0) return '0 Bytes';
        const k = 1024;
        const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.min(Math.floor(Math.log(bytes) / Math.log(k)), sizes.length - 1);
        return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
    }

    function formatParams(count) {
        if (count >= 1e9) return `${parseFloat((count / 1e9).toFixed(2))}B`;
        if (count >= 1e6) return `${parseFloat((count / 1e6).toFixed(1))}M`;
        return count.toLocaleString();
    }

    function formatDuration(seconds) {
        seconds = Math.ceil(seconds);
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        const s = seconds % 60;
        if (h) return `${h}h ${m}m`;
        if (m) return `${m}m ${s}s`;
        return `${s}s`;
    }

    function escapeHtml(value) {
        return String(value ?? '').replace(/[&<>"']/g, ch => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        })[ch]);
    }
});
