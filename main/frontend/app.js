let currentJob = null;
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
        pollJob();
    } catch (error) {
        setBusy(false, "");
        $("error").textContent = error.message;
    }
}

async function pollJob() {
    if (!currentJob) return;
    try {
        const response = await fetch("/api/jobs/" + currentJob);
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "Status request failed");
        setBusy(true, data.message || data.status);
        if (data.status === "queued" || data.status === "downloading" || data.status === "extracting") {
            setTimeout(pollJob, 2000);
            return;
        }
        setBusy(false, "");
        if (data.status === "error") throw new Error(data.message);
        if (data.status === "ready") showResult(data);
    } catch (error) {
        setBusy(false, "");
        $("error").textContent = error.message;
    }
}

function showResult(data) {
    currentJob = data.job_id;
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
    $("deleteBtn").style.display = instant ? "none" : "inline-block";
    $("streamHeading").textContent = instant
        ? (data.separate_streams ? "Source video URL (video only)" : "Direct source media URL")
        : "Stream URL · VLC / mpv / browser";
    $("resultNote").textContent = instant
        ? (data.separate_streams
            ? "The provider returned separate video and audio streams. These are not stored or merged, and may expire or require provider headers/cookies."
            : "This provider URL is not stored in the bucket. It may expire at any time or require provider headers/cookies.")
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
            '<button class="btn-ghost" data-act="copy">Copy URL</button>' +
            '<button class="btn-ghost" data-act="del">Delete</button></div>';
        row.querySelector(".lib-name").textContent = item.filename;
        row.querySelector('[data-act="open"]').onclick = () => showResult(item);
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
