document.addEventListener('DOMContentLoaded', function() {
    const modelInput = document.getElementById('modelInput');
    const checkBtn = document.getElementById('checkBtn');
    const resultsSection = document.getElementById('resultsSection');
    const recommendationsGrid = document.getElementById('recommendationsGrid');
    const kvCacheInfo = document.getElementById('kvCacheInfo');
    const contextLength = document.getElementById('contextLength');
    const contextValue = document.getElementById('contextValue');
    const deployBtn = document.getElementById('deployBtn');
    const pullBtn = document.getElementById('pullBtn');
    const quickDeployBtn = document.getElementById('quickDeployBtn');
    const deployStatus = document.getElementById('deployStatus');
    const modelsList = document.getElementById('modelsList');

    const QUANTIZATION_MAP = {
        'q4_0': { name: 'Q4_0', bits: 4 },
        'q4_1': { name: 'Q4_1', bits: 4 },
        'q5_0': { name: 'Q5_0', bits: 5 },
        'q5_1': { name: 'Q5_1', bits: 5 },
        'q8_0': { name: 'Q8_0', bits: 8 },
        'q2_k': { name: 'Q2_K', bits: 2 },
        'q3_k_m': { name: 'Q3_K_M', bits: 3 },
        'q6_k': { name: 'Q6_K', bits: 6 },
    };

    let currentModelInfo = null;
    let currentRecommendations = null;
    let selectedQuantization = null;

    checkOllamaStatus();
    loadExistingModels();

    checkBtn.addEventListener('click', checkModel);
    modelInput.addEventListener('keypress', function(e) {
        if (e.key === 'Enter') checkModel();
    });

    contextLength.addEventListener('input', function() {
        contextValue.textContent = this.value;
        updateDeployInfo();
    });

    deployBtn.addEventListener('click', deployModel);
    pullBtn.addEventListener('click', pullModel);
    quickDeployBtn.addEventListener('click', quickDeploy);

    function checkOllamaStatus() {
        fetch('/api/check-ollama')
            .then(res => res.json())
            .then(data => {
                const dot = document.getElementById('ollamaStatusDot');
                const status = document.getElementById('ollamaStatus');
                
                if (data.status === 'running') {
                    dot.classList.add('online');
                    status.textContent = `Ollama Running (v${data.version})`;
                } else if (data.status === 'offline') {
                    dot.classList.add('offline');
                    status.textContent = 'Ollama Offline';
                } else {
                    status.textContent = 'Ollama Error';
                }
            })
            .catch(err => {
                document.getElementById('ollamaStatusDot').classList.add('offline');
                document.getElementById('ollamaStatus').textContent = 'Ollama Offline';
            });
    }

    function loadExistingModels() {
        fetch('/api/list-models')
            .then(res => res.json())
            .then(data => {
                if (data.error) {
                    modelsList.innerHTML = '<p class="loading">Unable to load models</p>';
                    return;
                }
                
                if (!data.length) {
                    modelsList.innerHTML = '<p class="loading">No models installed</p>';
                    return;
                }

                modelsList.innerHTML = data.map(model => `
                    <div class="model-item">
                        <div>
                            <div class="model-name">${model.name}</div>
                            <div class="model-size">${formatBytes(model.size)}</div>
                        </div>
                        <button class="btn btn-danger" onclick="deleteModel('${model.name}')">Delete</button>
                    </div>
                `).join('');
            })
            .catch(err => {
                modelsList.innerHTML = '<p class="loading">Unable to load models</p>';
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

            if (data.error) {
                alert(`Error: ${data.error}`);
                return;
            }

            currentModelInfo = data;
            currentRecommendations = data.recommendations;
            displayModelInfo(data);
            displayRecommendations(data.recommendations);
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
        document.getElementById('modelId').textContent = data.model_info.id;
        document.getElementById('modelAuthor').textContent = data.model_info.author;
        document.getElementById('modelPipeline').textContent = data.model_info.pipeline_tag || 'N/A';
        document.getElementById('modelLikes').textContent = data.model_info.likes.toLocaleString();
        document.getElementById('modelDownloads').textContent = data.model_info.downloads.toLocaleString();
        
        document.getElementById('modelType').textContent = data.model_architecture.model_type || 'N/A';
        document.getElementById('modelArchitectures').textContent = (data.model_architecture.architectures || []).join(', ') || 'N/A';
        document.getElementById('modelHiddenSize').textContent = data.model_architecture.hidden_size || 'N/A';
        document.getElementById('modelLayers').textContent = data.model_architecture.num_hidden_layers || 'N/A';
        document.getElementById('modelHeads').textContent = data.model_architecture.num_attention_heads || 'N/A';
        document.getElementById('modelVocab').textContent = data.model_architecture.vocab_size || 'N/A';
        document.getElementById('modelMaxPos').textContent = data.model_architecture.max_position_embeddings || 'N/A';
        
        document.getElementById('modelTotalSize').textContent = formatBytes(data.model_sizes.total_size);
        document.getElementById('modelFileCount').textContent = data.model_sizes.file_count;
    }

    function displayRecommendations(recommendations) {
        if (!recommendations.length) {
            recommendationsGrid.innerHTML = '<p class="loading">No compatible quantizations found</p>';
            return;
        }

        recommendationsGrid.innerHTML = recommendations.map((rec, index) => {
            const qualityClass = rec.quality === 'High' || rec.quality === 'Very High' ? 'quality-high' :
                               rec.quality === 'Medium' ? 'quality-medium' : 'quality-low';
            const isSelected = selectedQuantization === rec.quantization;
            const isRecommended = rec.recommended && !selectedQuantization;
            
            if (isRecommended && !selectedQuantization) {
                selectedQuantization = rec.quantization;
            }

            return `
                <div class="quant-card ${rec.recommended ? 'recommended' : ''} ${isSelected ? 'selected' : ''}" 
                     onclick="selectQuantization('${rec.quantization}', '${rec.name}', ${rec.bits}, event)">
                    <div class="quant-header">
                        <span class="quant-name">${rec.name}</span>
                        <span class="quant-bits">${rec.bits}-bit</span>
                    </div>
                    <span class="quant-quality ${qualityClass}">${rec.quality}</span>
                    <p class="quant-description">${rec.description}</p>
                    <div class="quant-stats">
                        <div class="quant-stat">
                            <span class="stat-label">Size:</span>
                            <span class="stat-value">${rec.estimated_size_gb} GB</span>
                        </div>
                        <div class="quant-stat">
                            <span class="stat-label">VRAM:</span>
                            <span class="stat-value">${rec.vram_needed_gb} GB</span>
                        </div>
                        <div class="quant-stat">
                            <span class="stat-label">RAM:</span>
                            <span class="stat-value">${rec.ram_needed_gb} GB</span>
                        </div>
                        <div class="quant-stat">
                            <span class="stat-label">Speed:</span>
                            <span class="stat-value">${rec.speed}</span>
                        </div>
                    </div>
                    ${isSelected ? '<div class="selected-badge">✓ Selected</div>' : ''}
                </div>
            `;
        }).join('');

        updateQuickDeployInfo();
    }

    function displayKvCache(kvCache) {
        kvCacheInfo.innerHTML = `
            <div class="kv-cache-grid">
                <div class="kv-stat">
                    <div class="kv-stat-value">${kvCache.kv_cache_per_token_mb} MB</div>
                    <div class="kv-stat-label">Per Token</div>
                </div>
                <div class="kv-stat">
                    <div class="kv-stat-value">${kvCache.kv_cache_total_gb} GB</div>
                    <div class="kv-stat-label">Total KV Cache</div>
                </div>
                <div class="kv-stat">
                    <div class="kv-stat-value">${kvCache.max_context_length}</div>
                    <div class="kv-stat-label">Max Context</div>
                </div>
                <div class="kv-stat">
                    <div class="kv-stat-value">${kvCache.recommended_context_length}</div>
                    <div class="kv-stat-label">Recommended Context</div>
                </div>
            </div>
            <div class="context-slider">
                <label for="contextLength">Context Length:</label>
                <input type="range" id="contextLength" min="256" max="${kvCache.max_context_length}" value="${kvCache.recommended_context_length}" step="256">
                <span class="slider-value" id="contextValue">${kvCache.recommended_context_length}</span>
            </div>
        `;

        const newContextLength = document.getElementById('contextLength');
        const newContextValue = document.getElementById('contextValue');
        
        newContextLength.addEventListener('input', function() {
            newContextValue.textContent = this.value;
            updateDeployInfo();
        });
    }

    function selectQuantization(quant, name, bits, event) {
        selectedQuantization = quant;
        
        document.querySelectorAll('.quant-card').forEach(card => {
            card.classList.remove('selected');
        });
        
        if (event && event.currentTarget) {
            event.currentTarget.classList.add('selected');
        }
        
        updateDeployInfo();
    }

    function updateDeployInfo() {
        const contextLength = document.getElementById('contextLength') || { value: 4096 };
        const modelId = currentModelInfo ? currentModelInfo.model_info.id.split('/')[1] : 'Unknown';
        const quantName = selectedQuantization ? QUANTIZATION_MAP[selectedQuantization].name : 'Q4_0';
        
        document.getElementById('deployModelName').textContent = modelId;
        document.getElementById('deployQuantName').textContent = quantName;
        document.getElementById('deployContextLength').textContent = contextLength.value;
        
        updateQuickDeployInfo();
    }

    function updateQuickDeployInfo() {
        const quickDeployModel = document.getElementById('quickDeployModel');
        const quickDeployBtn = document.getElementById('quickDeployBtn');
        
        if (currentModelInfo && selectedQuantization) {
            const modelId = currentModelInfo.model_info.id.split('/')[1];
            const quantName = QUANTIZATION_MAP[selectedQuantization].name;
            quickDeployModel.textContent = `${modelId} (${quantName})`;
            quickDeployBtn.disabled = false;
        } else {
            quickDeployModel.textContent = 'No model selected';
            quickDeployBtn.disabled = true;
        }
    }

    async function quickDeploy() {
        if (!currentModelInfo || !selectedQuantization) {
            alert('Please check a model and select a quantization first');
            return;
        }

        const contextLength = document.getElementById('contextLength').value;
        const modelId = currentModelInfo.model_info.id;

        deployStatus.className = 'deploy-status loading';
        deployStatus.textContent = 'Quick deploying model...';
        deployBtn.disabled = true;
        pullBtn.disabled = true;
        quickDeployBtn.disabled = true;

        try {
            const response = await fetch('/api/deploy', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    model_id: modelId,
                    quantization: selectedQuantization,
                    context_length: contextLength
                })
            });

            const data = await response.json();

            if (data.success) {
                deployStatus.className = 'deploy-status success';
                deployStatus.textContent = `✓ Model deployed successfully as ${data.model_name}`;
                loadExistingModels();
            } else {
                deployStatus.className = 'deploy-status error';
                deployStatus.textContent = `✗ Deployment failed: ${data.error}`;
            }
        } catch (err) {
            deployStatus.className = 'deploy-status error';
            deployStatus.textContent = `✗ Error: ${err.message}`;
        } finally {
            deployBtn.disabled = false;
            pullBtn.disabled = false;
            quickDeployBtn.disabled = false;
        }
    }

    async function deployModel() {
        if (!currentModelInfo || !selectedQuantization) {
            alert('Please check a model and select a quantization first');
            return;
        }

        const contextLength = document.getElementById('contextLength').value;
        const modelId = currentModelInfo.model_info.id;

        deployStatus.className = 'deploy-status loading';
        deployStatus.textContent = 'Deploying model...';
        deployBtn.disabled = true;
        pullBtn.disabled = true;

        try {
            const response = await fetch('/api/deploy', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    model_id: modelId,
                    quantization: selectedQuantization,
                    context_length: contextLength
                })
            });

            const data = await response.json();

            if (data.success) {
                deployStatus.className = 'deploy-status success';
                deployStatus.textContent = `✓ Model deployed successfully as ${data.model_name}`;
                loadExistingModels();
            } else {
                deployStatus.className = 'deploy-status error';
                deployStatus.textContent = `✗ Deployment failed: ${data.error}`;
            }
        } catch (err) {
            deployStatus.className = 'deploy-status error';
            deployStatus.textContent = `✗ Error: ${err.message}`;
        } finally {
            deployBtn.disabled = false;
            pullBtn.disabled = false;
        }
    }

    async function pullModel() {
        if (!currentModelInfo) {
            alert('Please check a model first');
            return;
        }

        const quantization = selectedQuantization || 'q4_0';
        const modelId = currentModelInfo.model_info.id;

        deployStatus.className = 'deploy-status loading';
        deployStatus.textContent = 'Pulling model from Ollama...';
        deployBtn.disabled = true;
        pullBtn.disabled = true;

        try {
            const response = await fetch('/api/pull-model', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    model_id: modelId,
                    quantization: quantization
                })
            });

            const data = await response.json();

            if (data.success) {
                deployStatus.className = 'deploy-status success';
                deployStatus.textContent = `✓ Model pulled successfully as ${data.model_name}`;
                loadExistingModels();
            } else {
                deployStatus.className = 'deploy-status error';
                deployStatus.textContent = `✗ Pull failed: ${data.error}`;
            }
        } catch (err) {
            deployStatus.className = 'deploy-status error';
            deployStatus.textContent = `✗ Error: ${err.message}`;
        } finally {
            deployBtn.disabled = false;
            pullBtn.disabled = false;
        }
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

    function formatBytes(bytes) {
        if (bytes === 0) return '0 Bytes';
        const k = 1024;
        const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
    }

    window.deleteModel = deleteModel;
});
