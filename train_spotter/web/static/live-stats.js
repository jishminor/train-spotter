/**
 * Live statistics updater for Train Spotter
 * Polls /api/status endpoint and updates the stats cards on the live view
 */
(() => {
  const trainCountEl = document.getElementById("train-count");
  const vehicleCountEl = document.getElementById("vehicle-count");
  const lastTrainEl = document.getElementById("last-train");
  const connectionBadgeEl = document.getElementById("connection-badge");
  const streamStatusEl = document.getElementById("stream-status");
  const streamModeEl = document.getElementById("stream-mode");

  let pollInterval = null;

  // Helper to format relative time
  const formatRelativeTime = (isoString) => {
    if (!isoString) return "Never";
    const date = new Date(isoString);
    const now = new Date();
    const diffMs = now - date;
    const diffMins = Math.floor(diffMs / 60000);

    if (diffMins < 1) return "Just now";
    if (diffMins < 60) return `${diffMins}m ago`;
    const diffHours = Math.floor(diffMins / 60);
    if (diffHours < 24) return `${diffHours}h ago`;
    const diffDays = Math.floor(diffHours / 24);
    return `${diffDays}d ago`;
  };

  // Helper to update UI based on status
  const updateConnectionUI = (text, state = "connecting") => {
    // Update badge
    if (connectionBadgeEl) {
      let badgeClass = "bg-warning";
      let icon = "bi-hourglass-split";
      let label = "Connecting";

      if (state === "connected" || text.toLowerCase().includes("streaming")) {
        badgeClass = "bg-success";
        icon = "bi-circle-fill";
        label = "Live";
      } else if (state === "error" || text.toLowerCase().includes("error") || text.toLowerCase().includes("failed")) {
        badgeClass = "bg-danger";
        icon = "bi-x-circle-fill";
        label = "Error";
      }

      connectionBadgeEl.className = `badge ${badgeClass}`;
      connectionBadgeEl.innerHTML = `<i class="${icon}"></i> ${label}`;
    }

    // Update status text with icon
    if (streamStatusEl) {
      let iconClass = "bi-hourglass-split";
      let statusClass = "status-connecting";

      if (state === "connected" || text.toLowerCase().includes("streaming")) {
        iconClass = "bi-play-circle-fill";
        statusClass = "status-connected";
      } else if (state === "error" || text.toLowerCase().includes("error") || text.toLowerCase().includes("failed")) {
        iconClass = "bi-exclamation-triangle-fill";
        statusClass = "status-error";
      }

      streamStatusEl.className = `stream-status mb-0 ${statusClass}`;
      streamStatusEl.innerHTML = `<i class="${iconClass}"></i> ${text}`;
    }

    // Update stream mode indicator
    if (streamModeEl) {
      if (text.toLowerCase().includes("mjpeg")) {
        streamModeEl.textContent = "MJPEG Fallback";
      } else {
        streamModeEl.textContent = "WebRTC";
      }
    }
  };

  // Monitor status element for changes
  const monitorStatusChanges = () => {
    if (!streamStatusEl) return;

    const observer = new MutationObserver(() => {
      const text = streamStatusEl.textContent || streamStatusEl.innerText || "";
      let state = "connecting";
      if (text.toLowerCase().includes("streaming")) state = "connected";
      else if (text.toLowerCase().includes("error") || text.toLowerCase().includes("failed")) state = "error";
      updateConnectionUI(text, state);
    });

    observer.observe(streamStatusEl, {
      childList: true,
      characterData: true,
      subtree: true
    });
  };

  // Fetch and update stats
  const updateStats = async () => {
    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 5000); // 5 second timeout

      const response = await fetch("/api/status", {
        signal: controller.signal
      });
      clearTimeout(timeoutId);

      if (!response.ok) {
        console.warn("Failed to fetch stats:", response.status);
        return;
      }

      const data = await response.json();

      // Update train count
      if (trainCountEl) {
        trainCountEl.textContent = data.train_count || 0;
      }

      // Update vehicle count
      if (vehicleCountEl) {
        vehicleCountEl.textContent = data.vehicle_count || 0;
      }

      // Update last train time
      if (lastTrainEl && data.latest_train) {
        lastTrainEl.textContent = formatRelativeTime(data.latest_train.started_at);
      } else if (lastTrainEl) {
        lastTrainEl.textContent = "Never";
      }

    } catch (err) {
      if (err.name === 'AbortError') {
        console.warn("Stats fetch timed out");
      } else {
        console.warn("Error fetching stats:", err.message);
      }
    }
  };

  // Start polling
  const startPolling = () => {
    monitorStatusChanges(); // Start monitoring status changes

    // Delay initial stats fetch to not interfere with WebRTC setup
    setTimeout(() => {
      updateStats();
      pollInterval = setInterval(updateStats, 10000); // Poll every 10 seconds
    }, 3000); // Wait 3 seconds before first fetch
  };

  // Stop polling on page unload
  window.addEventListener("beforeunload", () => {
    if (pollInterval) {
      clearInterval(pollInterval);
    }
  });

  // Start when DOM is ready
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", startPolling);
  } else {
    startPolling();
  }
})();
