/**
 * Simplified WebRTC viewer for train-spotter.
 * Works with shared encoder pipeline - simpler client logic.
 */
(() => {
  const videoEl = document.getElementById("live-video");
  const canvasEl = document.getElementById("live-canvas");
  const canvasCtx = canvasEl ? canvasEl.getContext("2d") : null;
  const imageEl = document.getElementById("live-image");
  const statusEl = document.getElementById("stream-status");

  if (!statusEl) {
    return;
  }

  const setStatus = (text) => {
    statusEl.textContent = text;
  };

  const showVideo = () => {
    if (videoEl) videoEl.hidden = false;
    if (canvasEl) canvasEl.hidden = true;
    if (imageEl) imageEl.hidden = true;
  };

  const showFallbackSurface = () => {
    if (videoEl) {
      videoEl.hidden = true;
      videoEl.srcObject = null;
    }
    if (imageEl) {
      imageEl.hidden = false;
      if (canvasEl) canvasEl.hidden = true;
      return;
    }
    if (canvasEl) canvasEl.hidden = false;
  };

  // ---- WebRTC Configuration ----
  const rtcConfig = {
    iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
    iceCandidatePoolSize: 0
  };

  let pc = null;
  let ws = null;
  let reconnectTimer = null;
  let backoffMs = 1000;
  let webrtcDisabled = false;
  let fallbackActive = false;
  let webrtcRetryCount = 0;
  const maxWebRTCRetries = 5;
  const pendingCandidates = [];

  // ---- MJPEG Fallback ----
  let mjpegWs = null;
  let mjpegReconnectTimer = null;
  let mjpegBackoffMs = 1000;

  const wsUrl = (() => {
    const configured = window.TRAIN_SPOTTER && window.TRAIN_SPOTTER.signalingUrl;
    if (configured) return configured;
    const port = window.TRAIN_SPOTTER && window.TRAIN_SPOTTER.signalingPort;
    const hostOverride = window.TRAIN_SPOTTER && window.TRAIN_SPOTTER.signalingHost;
    const host = hostOverride || window.location.hostname;
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    if (port) return `${scheme}://${host}:${port}`;
    return `${scheme}://${window.location.host.replace(/\/$/, "")}/ws`;
  })();

  const mjpegUrl = (() => {
    const configured = window.TRAIN_SPOTTER && window.TRAIN_SPOTTER.mjpegUrl;
    if (configured) return configured;
    const port = window.TRAIN_SPOTTER && window.TRAIN_SPOTTER.mjpegPort;
    const hostOverride = window.TRAIN_SPOTTER && window.TRAIN_SPOTTER.mjpegHost;
    if (!port) return null;
    const host = hostOverride || window.location.hostname;
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    return `${scheme}://${host}:${port}/mjpeg`;
  })();

  const sendSignal = (payload) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(payload));
    }
  };

  const renderBitmap = async (blob) => {
    if (!canvasEl || !canvasCtx) return;
    try {
      if (typeof createImageBitmap === "function") {
        const bitmap = await createImageBitmap(blob);
        if (canvasEl.width !== bitmap.width || canvasEl.height !== bitmap.height) {
          canvasEl.width = bitmap.width;
          canvasEl.height = bitmap.height;
        }
        canvasCtx.drawImage(bitmap, 0, 0);
        bitmap.close();
        return;
      }
    } catch (err) {
      console.warn("createImageBitmap failed; falling back to Image() renderer", err);
    }

    await new Promise((resolve) => {
      const img = new Image();
      img.onload = () => {
        if (canvasEl.width !== img.naturalWidth || canvasEl.height !== img.naturalHeight) {
          canvasEl.width = img.naturalWidth;
          canvasEl.height = img.naturalHeight;
        }
        canvasCtx.drawImage(img, 0, 0);
        URL.revokeObjectURL(img.src);
        resolve();
      };
      img.onerror = () => {
        URL.revokeObjectURL(img.src);
        resolve();
      };
      img.src = URL.createObjectURL(blob);
    });
  };

  // ---- Simplified H.264 SDP manipulation ----
  function forceH264Only(sdp) {
    const lines = sdp.split(/\r?\n/);
    let mLineIdx = -1;
    const h264Pts = new Set();

    for (const l of lines) {
      const m = l.match(/^a=rtpmap:(\d+)\s+H264\/90000/i);
      if (m) h264Pts.add(m[1]);
    }
    if (h264Pts.size === 0) return sdp;

    for (let i = 0; i < lines.length; i++) {
      if (lines[i].startsWith("m=video")) {
        mLineIdx = i;
        const parts = lines[i].split(" ");
        const header = parts.slice(0, 3);
        const pts = parts.slice(3).filter(pt => h264Pts.has(pt));
        if (pts.length) lines[i] = [...header, ...pts].join(" ");
        break;
      }
    }
    if (mLineIdx === -1) return sdp;

    const kept = [];
    const ptRegex = /^a=(rtpmap|fmtp|rtcp-fb):(\d+)/i;
    for (const l of lines) {
      const m = l.match(ptRegex);
      if (!m || h264Pts.has(m[2])) kept.push(l);
    }
    return kept.join("\r\n");
  }

  // ---- WebRTC Connection ----
  const connect = () => {
    if (fallbackActive) return;
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
    setStatus("Connecting to signaling channel…");

    // Create peer connection
    pc = new RTCPeerConnection(rtcConfig);

    // Add transceiver for receiving video
    const tx = pc.addTransceiver("video", { direction: "recvonly" });

    // Prefer H.264
    try {
      const all = RTCRtpReceiver.getCapabilities("video").codecs;
      const h264 = all
        .filter(c => c.mimeType.toLowerCase() === "video/h264")
        .filter(c => /packetization-mode=1/.test(c.sdpFmtpLine || ""));
      const rtx = all.filter(c => c.mimeType.toLowerCase() === "video/rtx");
      if (h264.length) {
        tx.setCodecPreferences([...h264, ...rtx]);
      }
    } catch (e) {
      console.warn("setCodecPreferences not available; will try SDP-munge fallback.", e);
    }

    pc.ontrack = (event) => {
      console.log("WebRTC: Track received", event);
      if (event.streams && event.streams[0] && videoEl) {
        videoEl.srcObject = event.streams[0];
        const playPromise = videoEl.play?.();
        if (playPromise && typeof playPromise.catch === "function") {
          playPromise.catch((err) => {
            console.debug("Video play blocked, will retry after user interaction", err);
          });
        }
        showVideo();
        setStatus("Streaming");
        webrtcRetryCount = 0;
      }
    };

    pc.oniceconnectionstatechange = () => {
      console.log("WebRTC: ICE connection state:", pc.iceConnectionState);
      if (pc.iceConnectionState === "failed") {
        console.error("WebRTC: ICE connection failed");
        setStatus("ICE connection failed. Retrying…");
        scheduleReconnect(true);
      }
    };

    pc.onconnectionstatechange = () => {
      console.log("WebRTC: Connection state:", pc.connectionState);
      if (fallbackActive) return;
      if (pc.connectionState === "failed" || pc.connectionState === "disconnected") {
        setStatus("Peer connection dropped. Reconnecting…");
        scheduleReconnect(true);
      } else if (pc.connectionState === "connected") {
        setStatus("Streaming");
      }
    };

    pc.onicecandidate = (event) => {
      if (event.candidate) {
        console.log("WebRTC: Sending local ICE candidate");
        sendSignal({ type: "candidate", candidate: event.candidate });
      }
    };

    // WebSocket signaling
    ws = new WebSocket(wsUrl);
    pendingCandidates.length = 0;

    ws.onopen = async () => {
      try {
        setStatus("Negotiating WebRTC session…");
        console.log("WebRTC: Creating offer");

        let offer = await pc.createOffer();
        if (!/H264\/90000/.test(offer.sdp)) {
          offer = new RTCSessionDescription({
            type: "offer",
            sdp: forceH264Only(offer.sdp)
          });
          console.log("WebRTC: Applied SDP-munge to prefer only H.264");
        }

        await pc.setLocalDescription(offer);
        console.log("WebRTC: Local description set, sending offer");
        sendSignal({ type: "offer", sdp: offer.sdp });
        backoffMs = 1000;
      } catch (err) {
        console.error("Failed to create offer", err);
        setStatus("Negotiation error. Retrying…");
        scheduleReconnect(true);
      }
    };

    ws.onmessage = async (event) => {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch (err) {
        console.warn("Ignoring malformed signaling message", event.data);
        return;
      }

      if (message.type === "answer") {
        try {
          console.log("WebRTC: Received SDP answer", message.sdp);
          await pc.setRemoteDescription({ type: "answer", sdp: message.sdp });
          setStatus("Negotiating connection…");

          console.log(`WebRTC: Processing ${pendingCandidates.length} buffered ICE candidates...`);
          while (pendingCandidates.length > 0) {
            const candidate = pendingCandidates.shift();
            try {
              await pc.addIceCandidate(candidate);
            } catch (err) {
              console.warn("Failed to drain ICE candidate", err);
            }
          }
          console.log("WebRTC: All buffered candidates added");
        } catch (err) {
          console.error("Failed to set remote description", err);
          setStatus("Remote description error. Retrying…");
          scheduleReconnect(true);
        }
      } else if (message.type === "candidate" && message.candidate) {
        try {
          console.log("WebRTC: Received remote ICE candidate");
          if (pc.remoteDescription) {
            await pc.addIceCandidate(message.candidate);
          } else {
            console.log("WebRTC: Buffering ICE candidate (no remote description yet)");
            pendingCandidates.push(message.candidate);
          }
        } catch (err) {
          console.warn("Failed to add ICE candidate", err);
        }
      } else if (message.type === "error") {
        const reason = message.reason || "unknown";
        if (reason === "webrtc-unavailable") {
          setStatus("WebRTC unavailable. Switching to MJPEG fallback…");
          activateMjpegFallback();
        } else {
          setStatus(`Stream error: ${reason}. Retrying…`);
          scheduleReconnect(true);
        }
      } else if (message.type === "session-closed") {
        setStatus("Session ended. Reconnecting…");
        scheduleReconnect(true);
      }
    };

    ws.onerror = () => {
      setStatus("Signaling error. Retrying…");
      scheduleReconnect(true);
    };

    ws.onclose = () => {
      if (fallbackActive || webrtcDisabled) return;
      setStatus("Signaling channel closed. Reconnecting…");
      scheduleReconnect(true);
    };
  };

  const scheduleReconnect = (countRetry = false) => {
    if (fallbackActive || webrtcDisabled) return;
    if (reconnectTimer) return;
    if (countRetry) {
      webrtcRetryCount += 1;
      if (webrtcRetryCount >= maxWebRTCRetries) {
        setStatus("WebRTC retries exhausted. Switching to MJPEG fallback…");
        activateMjpegFallback();
        return;
      }
    }
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.close();
    }
    backoffMs = Math.min(backoffMs * 1.5, 10000);
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      try {
        if (pc && pc.signalingState !== "closed") {
          pc.restartIce();
        }
      } catch (err) {
        console.debug("restartIce unsupported in current state", err);
      }
      connect();
    }, backoffMs);
  };

  // ---- MJPEG Fallback ----
  const stopMjpegStream = () => {
    if (mjpegWs) {
      try {
        mjpegWs.close();
      } catch (_) {}
      mjpegWs = null;
    }
    if (mjpegReconnectTimer) {
      clearTimeout(mjpegReconnectTimer);
      mjpegReconnectTimer = null;
    }
    if (imageEl) imageEl.removeAttribute("src");
  };

  const scheduleMjpegReconnect = () => {
    if (!fallbackActive) return;
    if (mjpegReconnectTimer) return;
    mjpegBackoffMs = Math.min(mjpegBackoffMs * 1.5, 10000);
    mjpegReconnectTimer = setTimeout(() => {
      mjpegReconnectTimer = null;
      startMjpegStream();
    }, mjpegBackoffMs);
  };

  const startMjpegStream = () => {
    if (!mjpegUrl || !canvasEl || !canvasCtx) {
      setStatus("MJPEG fallback unavailable.");
      return;
    }
    stopMjpegStream();
    mjpegBackoffMs = 1000;
    mjpegWs = new WebSocket(mjpegUrl);
    mjpegWs.binaryType = "arraybuffer";

    mjpegWs.onopen = () => {
      setStatus("Streaming (MJPEG fallback)");
      showFallbackSurface();
    };

    mjpegWs.onmessage = async (event) => {
      try {
        const data = event.data;
        if (!data) {
          console.warn("MJPEG: Received empty frame");
          return;
        }
        const blob = data instanceof Blob ? data : new Blob([data], { type: "image/jpeg" });

        if (imageEl) {
          try {
            const url = URL.createObjectURL(blob);
            imageEl.onload = () => URL.revokeObjectURL(url);
            imageEl.onerror = (err) => {
              URL.revokeObjectURL(url);
              console.error("Failed to load MJPEG frame in img element", err);
            };
            imageEl.src = url;
            return;
          } catch (err) {
            console.warn("Failed to render MJPEG to img element", err);
          }
        }

        if (canvasEl && canvasCtx) {
          await renderBitmap(blob);
        }
      } catch (err) {
        console.error("Failed to process MJPEG frame", err);
      }
    };

    mjpegWs.onerror = () => {
      setStatus("MJPEG stream error. Retrying…");
      scheduleMjpegReconnect();
    };

    mjpegWs.onclose = () => {
      if (!fallbackActive) return;
      setStatus("MJPEG stream closed. Reconnecting…");
      scheduleMjpegReconnect();
    };
  };

  const activateMjpegFallback = () => {
    if (fallbackActive) return;
    console.warn("Activating MJPEG fallback");
    fallbackActive = true;
    webrtcDisabled = true;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.close();
    }
    ws = null;
    try {
      if (pc) pc.close();
    } catch (err) {
      console.debug("Failed to close RTCPeerConnection", err);
    }
    backoffMs = 1000;
    showFallbackSurface();
    if (videoEl) {
      try {
        videoEl.pause();
      } catch (_) {}
    }
    startMjpegStream();
  };

  // ---- Cleanup ----
  window.addEventListener("beforeunload", () => {
    if (ws && ws.readyState === WebSocket.OPEN) ws.close();
    stopMjpegStream();
    try {
      if (pc) pc.close();
    } catch (err) {
      console.debug("Failed to close peer connection on unload", err);
    }
  });

  if (videoEl) {
    videoEl.addEventListener("loadedmetadata", () => {
      console.log("WebRTC: video metadata", videoEl.videoWidth, videoEl.videoHeight);
    });
    videoEl.addEventListener("loadeddata", () => {
      console.log("WebRTC: first frame rendered", videoEl.videoWidth, videoEl.videoHeight);
    });
    videoEl.addEventListener("error", (err) => {
      console.error("WebRTC: video element error", err);
    });
  }

  if (videoEl) showVideo();
  connect();
})();
