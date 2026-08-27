document.addEventListener('DOMContentLoaded', () => {
    const socket = io();
    const form = document.getElementById('download-form');
    const downloadBtn = document.getElementById('download-btn');
    const urlInput = document.getElementById('video-url');
    const qualityMode = document.getElementById('quality-mode');
    const manualControls = document.getElementById('manual-quality-controls');
    const qualitySelect = document.getElementById('quality-select');
    const fileFormatSelect = document.getElementById('file-format-select');
    const fetchFormatsBtn = document.getElementById('fetch-formats-btn');
    const formatSummary = document.getElementById('format-summary');
    const activeContainer = document.getElementById('active-downloads');
    const historyContainer = document.getElementById('download-history');
    const historyCount = document.getElementById('history-count');
    let inspectedUrl = '';

    function showNotice(message, type = 'danger', duration = 7000) {
        const container = document.getElementById('notice-container');
        document.getElementById('notice-message').textContent = message;
        container.className = `alert alert-${type}`;
        if (duration) window.setTimeout(() => container.classList.add('d-none'), duration);
    }

    function validUrl() {
        const url = urlInput.value.trim();
        if (!url || !urlInput.checkValidity()) {
            showNotice('Enter a valid video page URL first.');
            urlInput.focus();
            return null;
        }
        return url;
    }

    function setInspecting(isInspecting) {
        fetchFormatsBtn.disabled = isInspecting;
        qualitySelect.disabled = isInspecting;
        fetchFormatsBtn.querySelector('i').classList.toggle('fa-spin', isInspecting);
    }

    async function fetchFormats() {
        const url = validUrl();
        if (!url) return false;

        setInspecting(true);
        qualitySelect.innerHTML = '<option value="">Inspecting page…</option>';
        formatSummary.classList.add('d-none');
        try {
            const response = await fetch('/api/formats', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({url})
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Could not inspect this page.');

            qualitySelect.replaceChildren();
            const placeholder = new Option('Select a quality', '');
            placeholder.disabled = true;
            placeholder.selected = true;
            qualitySelect.add(placeholder);
            data.qualities.forEach((quality) => {
                const fps = quality.fps ? ` · up to ${quality.fps} fps` : '';
                qualitySelect.add(new Option(`${quality.label}${fps}`, quality.value));
            });
            qualitySelect.disabled = false;
            inspectedUrl = url;
            formatSummary.textContent = `${data.title} · ${data.qualities.length} ${data.qualities.length === 1 ? 'quality' : 'qualities'} available`;
            formatSummary.classList.remove('d-none');
            return true;
        } catch (error) {
            inspectedUrl = '';
            qualitySelect.innerHTML = '<option value="">No qualities loaded</option>';
            showNotice(error.message);
            return false;
        } finally {
            setInspecting(false);
        }
    }

    qualityMode.addEventListener('change', () => {
        const manual = qualityMode.value === 'manual';
        manualControls.classList.toggle('d-none', !manual);
        if (manual && urlInput.value.trim()) fetchFormats();
    });

    urlInput.addEventListener('input', () => {
        if (urlInput.value.trim() !== inspectedUrl) {
            inspectedUrl = '';
            qualitySelect.innerHTML = '<option value="">Fetch qualities first</option>';
            qualitySelect.disabled = true;
            formatSummary.classList.add('d-none');
        }
    });

    fetchFormatsBtn.addEventListener('click', fetchFormats);

    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const url = validUrl();
        if (!url) return;

        if (qualityMode.value === 'manual' && inspectedUrl !== url) {
            const loaded = await fetchFormats();
            if (loaded) showNotice('Qualities loaded. Choose one, then click Download again.', 'info');
            return;
        }
        if (qualityMode.value === 'manual' && !qualitySelect.value) {
            showNotice('Choose one of the available qualities.');
            qualitySelect.focus();
            return;
        }

        downloadBtn.disabled = true;
        try {
            const response = await fetch('/api/download', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    url,
                    quality: qualityMode.value === 'auto' ? 'auto' : qualitySelect.value,
                    file_format: fileFormatSelect.value
                })
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Could not start the download.');
            updateDownload(data.job_id, data);
            showNotice('Download started.', 'success', 3000);
        } catch (error) {
            showNotice(error.message);
        } finally {
            downloadBtn.disabled = false;
        }
    });

    socket.on('download_progress', ({job_id, progress}) => updateDownload(job_id, progress));
    socket.on('download_complete', (video) => {
        updateDownload(video.id, {title: video.title, status: 'Complete', progress: 100});
        window.setTimeout(() => {
            document.getElementById(`download-${video.id}`)?.remove();
            ensureActiveEmptyState();
            fetchDownloadHistory();
        }, 2500);
    });
    socket.on('download_error', ({job_id, error}) => {
        const item = document.getElementById(`download-${job_id}`);
        if (item) {
            item.querySelector('.status-badge').textContent = 'Error';
            item.querySelector('.progress-bar').classList.add('bg-danger');
        }
        showNotice(error);
    });
    socket.on('connect_error', () => showNotice('Lost the live server connection. Refresh the page to reconnect.'));

    function ensureActiveEmptyState() {
        if (!activeContainer.querySelector('.download-item')) {
            activeContainer.innerHTML = '<p class="empty-state text-muted text-center mb-0">No active downloads</p>';
        }
    }

    function createDownload(jobId, data) {
        activeContainer.querySelector('.empty-state')?.remove();
        const item = document.createElement('div');
        item.id = `download-${jobId}`;
        item.className = 'download-item';
        item.innerHTML = `
            <div class="d-flex justify-content-between gap-3 align-items-start mb-2">
                <h3 class="video-title h6 mb-0"></h3>
                <span class="status-badge badge bg-primary"></span>
            </div>
            <div class="progress mb-2" role="progressbar" aria-valuemin="0" aria-valuemax="100">
                <div class="progress-bar" style="width:0%"></div>
            </div>
            <div class="download-details small text-muted">
                <span>0%</span><span class="speed"></span><span class="eta"></span><span class="size"></span>
            </div>`;
        activeContainer.prepend(item);
        return item;
    }

    function updateDownload(jobId, data = {}) {
        const item = document.getElementById(`download-${jobId}`) || createDownload(jobId, data);
        item.querySelector('.video-title').textContent = data.title || 'Inspecting video…';
        item.querySelector('.status-badge').textContent = data.status || 'Queued';
        const progress = Number(data.progress) || 0;
        const bar = item.querySelector('.progress-bar');
        bar.style.width = `${progress}%`;
        bar.parentElement.setAttribute('aria-valuenow', String(progress));
        const details = item.querySelector('.download-details');
        details.children[0].textContent = `${progress.toFixed(1)}%`;
        details.querySelector('.speed').textContent = data.speed ? formatSpeed(data.speed) : '';
        details.querySelector('.eta').textContent = data.eta ? `ETA ${formatTime(data.eta)}` : '';
        details.querySelector('.size').textContent = data.total_bytes ? `${formatBytes(data.downloaded_bytes)} / ${formatBytes(data.total_bytes)}` : '';
    }

    function formatBytes(bytes) {
        if (!bytes) return '0 B';
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
        return `${(bytes / (1024 ** index)).toFixed(index ? 1 : 0)} ${units[index]}`;
    }

    function formatSpeed(bytes) {
        return `${formatBytes(bytes)}/s`;
    }

    function formatTime(seconds) {
        const minutes = Math.floor(seconds / 60);
        return `${minutes}:${Math.floor(seconds % 60).toString().padStart(2, '0')}`;
    }

    async function fetchDownloadHistory() {
        try {
            const response = await fetch('/api/downloads');
            const data = await response.json();
            Object.entries(data.active || {}).forEach(([jobId, progress]) => updateDownload(jobId, progress));
            renderHistory(data.history || []);
        } catch (error) {
            showNotice('Could not load download history.');
        }
    }

    function renderHistory(history) {
        historyContainer.replaceChildren();
        historyCount.textContent = history.length;
        if (!history.length) {
            const empty = document.createElement('p');
            empty.className = 'text-muted text-center mb-0';
            empty.textContent = 'No downloads yet';
            historyContainer.append(empty);
            return;
        }

        [...history].reverse().forEach((video) => {
            const col = document.createElement('div');
            col.className = 'col-md-6';
            const card = document.createElement('article');
            card.className = 'history-card h-100';
            const title = document.createElement('h3');
            title.className = 'h6 text-truncate';
            title.title = video.title;
            title.textContent = video.title;
            const details = document.createElement('p');
            details.className = 'small text-muted mb-3';
            details.textContent = `${video.resolution} · ${video.file_format} · ${new Date(video.download_time).toLocaleString()}`;
            const actions = document.createElement('div');
            actions.className = 'd-flex gap-2';
            const save = document.createElement('a');
            save.className = 'btn btn-primary btn-sm';
            save.href = video.download_url;
            save.textContent = 'Save file';
            const source = document.createElement('a');
            source.className = 'btn btn-outline-secondary btn-sm';
            source.href = video.url;
            source.target = '_blank';
            source.rel = 'noopener noreferrer';
            source.textContent = 'View source';
            actions.append(save, source);
            card.append(title, details, actions);
            col.append(card);
            historyContainer.append(col);
        });
    }

    fetchDownloadHistory();
});
