let currentJob = null;
let currentMedia = null;
let activeUpload = null;
let transferEpoch = 0;
const $ = id => document.getElementById(id);

function toast(msg) {
    const el = $("toast");
    el.textContent = msg;
    el.classList.add("show");
    setTimeout(() => el.classList.remove("show"), 1800);
}

function formatBytes(bytes) {
    if (!bytes) return "";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
    return bytes.toFixed(2) + " " + units[i];
}

function formatExpiry(timestamp) {
    if (!timestamp) return "";
    return "Expires " + new Date(timestamp * 1000).toLocaleString();
}

function formatDuration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return "";
    if (seconds < 60) return Math.ceil(seconds) + "s";
    const minutes = Math.floor(seconds / 60);
    const remainder = Math.ceil(seconds % 60);
    return minutes + "m " + remainder + "s";
}

function sleep(milliseconds) {
    return new Promise(resolve => setTimeout(resolve, milliseconds));
}

function setUploadBusy(busy) {
    $("uploadBtn").disabled = busy;
    $("localFile").disabled = busy;
    $("uploadSpinner").classList.toggle("on", busy);
    $("cancelUploadBtn").style.display = busy ? "block" : "none";
}

function sendUploadChunk(url, blob, password, upload, onProgress) {
    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        upload.requests.add(xhr);
        xhr.open("PUT", url);
        xhr.timeout = 10 * 60 * 1000;
        xhr.setRequestHeader("Content-Type", "application/octet-stream");
        xhr.setRequestHeader("X-App-Password", password);
        xhr.upload.onprogress = event => {
            if (event.lengthComputable) onProgress(event.loaded);
        };
        xhr.onload = () => {
            upload.requests.delete(xhr);
            let data = {};
            try { data = JSON.parse(xhr.responseText || "{}"); } catch (_) { /* handled below */ }
            if (xhr.status >= 200 && xhr.status < 300) resolve(data);
            else reject(new Error(data.detail || "Upload chunk failed"));
        };
        xhr.onerror = () => {
            upload.requests.delete(xhr);
            reject(new Error("Network error while uploading"));
        };
        xhr.ontimeout = () => {
            upload.requests.delete(xhr);
            reject(new Error("Upload chunk timed out"));
        };
        xhr.onabort = () => {
            upload.requests.delete(xhr);
            reject(new Error("Upload cancelled"));
        };
        xhr.send(blob);
    });
}

async function uploadLocalFile() {
    const file = $("localFile").files[0];
    const password = $("password").value;
    if (!password) { $("uploadError").textContent = "Enter your password first"; return; }
    if (!file) { $("uploadError").textContent = "Choose a file first"; return; }

    sessionStorage.setItem("app_password", password);
    $("uploadError").textContent = "";
    $("result").style.display = "none";
    $("uploadProgress").style.display = "block";
    $("uploadProgressBar").style.width = "0%";
    $("uploadStatus").textContent = "Preparing upload...";
    setUploadBusy(true);

    let upload = null;
    try {
        const response = await fetch("/api/uploads", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ filename: file.name, size: file.size, password })
        });
        const config = await response.json();
        if (!response.ok) throw new Error(config.detail || "Could not start upload");

        upload = {
            jobId: config.job_id,
            cancelled: false,
            requests: new Set(),
            loaded: new Array(config.chunk_count).fill(0),
            completed: new Array(config.chunk_count).fill(0),
            started: performance.now()
        };
        activeUpload = upload;
        let nextChunk = 0;

        const updateProgress = () => {
            const transferred = upload.loaded.reduce((sum, value) => sum + value, 0) +
                upload.completed.reduce((sum, value) => sum + value, 0);
            const elapsed = Math.max((performance.now() - upload.started) / 1000, .1);
            const speed = transferred / elapsed;
            const eta = speed ? (file.size - transferred) / speed : Infinity;
            const percent = Math.min(100, transferred * 100 / file.size);
            $("uploadProgressBar").style.width = percent.toFixed(2) + "%";
            $("uploadStatus").textContent = formatBytes(transferred) + " / " + formatBytes(file.size) +
                " | " + formatBytes(speed) + "/s" + (transferred ? " | " + formatDuration(eta) + " left" : "");
        };

        const uploadOne = async index => {
            const start = index * config.chunk_size;
            const end = Math.min(start + config.chunk_size, file.size);
            const blob = file.slice(start, end);
            let lastError;
            for (let attempt = 0; attempt < 4; attempt++) {
                if (upload.cancelled) throw new Error("Upload cancelled");
                upload.loaded[index] = 0;
                updateProgress();
                try {
                    await sendUploadChunk(
                        "/api/uploads/" + upload.jobId + "/chunks/" + index,
                        blob,
                        password,
                        upload,
                        loaded => { upload.loaded[index] = loaded; updateProgress(); }
                    );
                    upload.loaded[index] = 0;
                    upload.completed[index] = blob.size;
                    updateProgress();
                    return;
                } catch (error) {
                    lastError = error;
                    if (upload.cancelled || attempt === 3) break;
                    await sleep(500 * 2 ** attempt);
                }
            }
            throw lastError;
        };

        const worker = async () => {
            while (!upload.cancelled) {
                const index = nextChunk++;
                if (index >= config.chunk_count) return;
                await uploadOne(index);
            }
        };
        await Promise.all(Array.from({ length: config.concurrency }, () => worker()));
        if (upload.cancelled) throw new Error("Upload cancelled");

        $("uploadStatus").textContent = "Finalizing...";
        const completeResponse = await fetch("/api/uploads/" + upload.jobId + "/complete", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ password })
        });
        const data = await completeResponse.json();
        if (!completeResponse.ok) throw new Error(data.detail || "Could not finalize upload");
        $("uploadProgressBar").style.width = "100%";
        $("uploadStatus").textContent = "Uploaded " + formatBytes(file.size);
        showResult(data);
        loadLibrary();
    } catch (error) {
        if (upload) {
            upload.cancelled = true;
            for (const request of upload.requests) request.abort();
        }
        if (upload && upload.jobId) {
            fetch("/api/uploads/" + upload.jobId + "/abort", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ password })
            }).catch(() => {});
        }
        $("uploadError").textContent = error.message;
        $("uploadStatus").textContent = "";
    } finally {
        if (activeUpload === upload) activeUpload = null;
        setUploadBusy(false);
    }
}

async function cancelUpload() {
    const upload = activeUpload;
    if (!upload) return;
    upload.cancelled = true;
    for (const request of upload.requests) request.abort();
    $("uploadStatus").textContent = "Cancelling...";
    try {
        await fetch("/api/uploads/" + upload.jobId + "/abort", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ password: $("password").value })
        });
    } catch (_) { /* stale uploads are cleaned up by the server */ }
}

function selectedDelivery() {
    return document.querySelector('input[name="delivery"]:checked').value;
}

function updateDeliveryOptions() {
    const instant = selectedDelivery() === "instant";
    const conversionModes = document.querySelectorAll('input[name="mode"][value="mp3"], input[name="mode"][value="m4a"]');
    for (const input of conversionModes) input.disabled = instant;
    const selectedMode = document.querySelector('input[name="mode"]:checked');
    if (instant && selectedMode.disabled) {
        document.querySelector('input[name="mode"][value="audio"]').checked = true;
    }
    $("modeNote").style.display = instant ? "block" : "none";
    $("goBtn").textContent = instant ? "Generate Instant Link" : "Download and Store Media";
}

function setBusy(busy, message) {
    $("spinner").classList.toggle("on", busy);
    $("goBtn").disabled = busy;
    $("status").textContent = message || "";
}

async function cancelAllTransfers() {
    const button = $("cancelAllBtn");
    button.disabled = true;
    const originalText = button.textContent;
    button.textContent = "Stopping...";
    transferEpoch++;
    currentJob = null;

    if (activeUpload) {
        activeUpload.cancelled = true;
        for (const request of activeUpload.requests) request.abort();
        $("uploadStatus").textContent = "Cancelling...";
    }
    setBusy(false, "");
    try {
        const response = await fetch("/api/transfers/cancel", { method: "POST" });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "Could not stop transfers");
        const count = Number(data.cancelled_jobs) || 0;
        $("status").textContent = count ? "All transfers cancelled" : "No active transfers";
        toast(count ? "All transfers stopped" : "No active transfers");
    } catch (error) {
        $("error").textContent = error.message;
    } finally {
        button.disabled = false;
        button.textContent = originalText;
    }
}

async function createJob() {
    const password = $("password").value;
    const url = $("url").value.trim();
    const mode = document.querySelector('input[name="mode"]:checked').value;
    const delivery = selectedDelivery();
    sessionStorage.setItem("app_password", password);
    $("error").textContent = "";
    $("result").style.display = "none";
    setBusy(true, "Creating job...");
    try {
        const response = await fetch("/api/jobs", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ password, url, mode, delivery })
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "Request failed");
        currentJob = data.job_id;
        pollJob(currentJob, transferEpoch);
    } catch (error) {
        setBusy(false, "");
        $("error").textContent = error.message;
    }
}

async function pollJob(jobId, epoch) {
    if (!jobId || epoch !== transferEpoch) return;
    try {
        const response = await fetch("/api/jobs/" + jobId);
        const data = await response.json();
        if (epoch !== transferEpoch) return;
        if (!response.ok) throw new Error(data.detail || "Status request failed");
        setBusy(true, data.message || data.status);
        if (data.status === "queued" || data.status === "downloading" || data.status === "extracting") {
            setTimeout(() => pollJob(jobId, epoch), 2000);
            return;
        }
        setBusy(false, "");
        if (data.status === "error") throw new Error(data.message);
        if (data.status === "cancelled") throw new Error("Transfer cancelled");
        if (data.status === "ready") showResult(data);
    } catch (error) {
        setBusy(false, "");
        $("error").textContent = error.message;
    }
}

function normalDownload(item) {
    const anchor = document.createElement("a");
    anchor.href = item.download_url;
    anchor.download = item.filename || "download";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
}

async function fastDownloadCurrent() {
    if (currentMedia) await fastDownload(currentMedia);
}

async function fastDownload(item) {
    if (!item || !item.download_url) return;
    if (!("showSaveFilePicker" in window) || !window.isSecureContext) {
        normalDownload(item);
        return;
    }

    const button = $("fastDownloadBtn");
    const originalText = button.textContent;
    let writable;
    try {
        const handle = await window.showSaveFilePicker({
            suggestedName: item.filename || "download"
        });
        writable = await handle.createWritable();
        let size = Number(item.size) || 0;
        if (!size) {
            const head = await fetch(item.download_url, { method: "HEAD" });
            if (!head.ok) throw new Error("Could not read the download size");
            size = Number(head.headers.get("Content-Length")) || 0;
        }
        if (!size) throw new Error("The download size is unknown");

        button.disabled = true;
        const partSize = 32 * 1024 * 1024;
        const partCount = Math.ceil(size / partSize);
        const concurrency = Math.min(6, partCount);
        let nextPart = 0;
        let downloaded = 0;
        let writeChain = Promise.resolve();
        const started = performance.now();

        const downloadPart = async index => {
            const start = index * partSize;
            const end = Math.min(start + partSize, size) - 1;
            let lastError;
            for (let attempt = 0; attempt < 4; attempt++) {
                try {
                    const response = await fetch(item.download_url, {
                        headers: { Range: "bytes=" + start + "-" + end }
                    });
                    if (response.status !== 206) throw new Error("Server did not honor the byte range");
                    const data = await response.arrayBuffer();
                    if (data.byteLength !== end - start + 1) throw new Error("Download part is incomplete");
                    const queuedWrite = writeChain.then(() => writable.write({ type: "write", position: start, data }));
                    writeChain = queuedWrite.catch(() => {});
                    await queuedWrite;
                    downloaded += data.byteLength;
                    const elapsed = Math.max((performance.now() - started) / 1000, .1);
                    button.textContent = Math.floor(downloaded * 100 / size) + "% | " +
                        formatBytes(downloaded / elapsed) + "/s";
                    return;
                } catch (error) {
                    lastError = error;
                    if (attempt < 3) await sleep(500 * 2 ** attempt);
                }
            }
            throw lastError;
        };

        const worker = async () => {
            while (true) {
                const index = nextPart++;
                if (index >= partCount) return;
                await downloadPart(index);
            }
        };
        await Promise.all(Array.from({ length: concurrency }, () => worker()));
        await writeChain;
        await writable.close();
        writable = null;
        toast("Download complete");
    } catch (error) {
        if (writable) {
            try { await writable.abort(); } catch (_) { /* best effort */ }
        }
        if (error.name !== "AbortError") toast(error.message || "Fast download failed");
    } finally {
        button.disabled = false;
        button.textContent = originalText;
    }
}

function showResult(data) {
    currentJob = data.job_id;
    currentMedia = data;
    $("result").style.display = "block";
    $("filename").textContent = data.filename;
    $("filesize").textContent = formatBytes(data.size);
    $("filesize").style.display = data.size ? "inline-block" : "none";
    $("filemode").textContent = (data.mode || "media").toUpperCase();
    const instant = data.delivery === "instant" || data.stored === false;
    $("expiry").textContent = instant
        ? (data.source_expires ? formatExpiry(data.source_expires) : "Provider expiry unknown")
        : formatExpiry(data.retention_expires || data.expires);
    $("streamUrl").value = data.stream_url;
    $("downloadUrl").value = data.download_url;
    $("audioUrl").value = data.audio_url || "";
    $("audioLinkRow").style.display = instant && data.separate_streams && data.audio_url ? "block" : "none";
    $("downloadRow").style.display = instant ? "none" : "block";
    $("fastDownloadBtn").style.display = instant ? "none" : "block";
    $("deleteBtn").style.display = instant ? "none" : "inline-block";
    $("streamHeading").textContent = instant
        ? "Direct source media URL"
        : "Stream URL · VLC / mpv / browser";
    $("resultNote").textContent = instant
        ? "This single-file provider URL is not stored in the bucket. It may expire at any time or require provider headers/cookies."
        : "Stored in the bucket until the displayed retention deadline.";
    const name = data.filename.toLowerCase();
    const video = $("videoPlayer");
    const audio = $("audioPlayer");
    video.style.display = "none";
    audio.style.display = "none";
    video.removeAttribute("src");
    audio.removeAttribute("src");
    if (data.mode === "audio") {
        audio.src = data.stream_url;
        audio.style.display = "block";
    } else if (/\.(mp4|webm|mkv|mov)$/.test(name)) {
        video.src = data.stream_url;
        video.style.display = "block";
    } else if (/\.(mp3|m4a|opus|ogg|wav|flac)$/.test(name)) {
        audio.src = data.stream_url;
        audio.style.display = "block";
    }
    $("result").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function deleteMedia(jobId) {
    const id = jobId || currentJob;
    if (!id) return;
    const password = $("password").value;
    const response = await fetch("/api/jobs/" + id + "/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password })
    });
    if (!response.ok) {
        const data = await response.json();
        toast(data.detail || "Delete failed");
        return;
    }
    toast("Deleted");
    if (id === currentJob) {
        $("result").style.display = "none";
        currentJob = null;
    }
    loadLibrary();
}

function copyField(id) {
    navigator.clipboard.writeText($(id).value).then(() => toast("Copied to clipboard"));
}

async function loadLibrary() {
    const password = $("password").value;
    if (!password) { toast("Enter your password first"); return; }
    sessionStorage.setItem("app_password", password);
    try {
        const response = await fetch("/api/library", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ password })
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "Failed to load library");
        renderLibrary(data.items);
    } catch (error) {
        $("library").innerHTML = '<div class="empty"></div>';
        $("library").firstChild.textContent = error.message;
    }
}

function renderLibrary(items) {
    const container = $("library");
    container.innerHTML = "";
    if (!items.length) {
        container.innerHTML = '<div class="empty">No media stored yet.</div>';
        return;
    }
    for (const item of items) {
        const row = document.createElement("div");
        row.className = "lib-item";
        const date = item.created ? new Date(item.created * 1000).toLocaleDateString() : "";
        const expiry = formatExpiry(item.retention_expires || item.expires);
        row.innerHTML =
            '<div class="lib-info"><div class="lib-name"></div>' +
            '<div class="lib-meta">' + formatBytes(item.size) + (date ? " · Added " + date : "") +
            (expiry ? " · " + expiry : "") + "</div></div>" +
            '<div class="lib-actions">' +
            '<button class="btn-ghost" data-act="open">Open</button>' +
            '<button class="btn-ghost" data-act="download">Fast Download</button>' +
            '<button class="btn-ghost" data-act="copy">Copy URL</button>' +
            '<button class="btn-ghost" data-act="del">Delete</button></div>';
        row.querySelector(".lib-name").textContent = item.filename;
        row.querySelector('[data-act="open"]').onclick = () => showResult(item);
        row.querySelector('[data-act="download"]').onclick = () => fastDownload(item);
        row.querySelector('[data-act="copy"]').onclick = () => {
            navigator.clipboard.writeText(item.download_url).then(() => toast("Copied to clipboard"));
        };
        row.querySelector('[data-act="del"]').onclick = () => deleteMedia(item.job_id);
        container.appendChild(row);
    }
}

window.addEventListener("DOMContentLoaded", () => {
    for (const input of document.querySelectorAll('input[name="delivery"]')) {
        input.addEventListener("change", updateDeliveryOptions);
    }
    updateDeliveryOptions();
    const saved = sessionStorage.getItem("app_password");
    if (saved) { $("password").value = saved; loadLibrary(); }
});
