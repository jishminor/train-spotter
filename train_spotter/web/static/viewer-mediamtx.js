/**
 * MediaMTX WebRTC viewer for train-spotter.
 * Uses MediaMTX's WHEP protocol for WebRTC streaming.
 */
(() => {
  const videoEl = document.getElementById("live-video");
  const canvasEl = document.getElementById("live-canvas");
  const canvasCtx = canvasEl ? canvasEl.getContext("2d") : null;
  const imageEl = document.getElementById("live-image");
  const statusEl = document.getElementById("stream-status");
  const fpsEl = document.getElementById("stream-fps");

  if (!statusEl) {
    return;
  }

  const setStatus = (text) => {
    statusEl.textContent = text;
  };

  const updateFpsDisplay = (text) => {
    if (fpsEl) {
      fpsEl.textContent = text;
    }
  };

  updateFpsDisplay("FPS: --");

  let pc = null;
  let webrtcStatsTimer = null;
  let lastInboundVideoStats = null;
  let lastRenderedFrames = null;
  let lastRenderedSampleTime = null;

  const stopWebRtcStats = () => {
    if (webrtcStatsTimer) {
      clearInterval(webrtcStatsTimer);
      webrtcStatsTimer = null;
    }
    lastInboundVideoStats = null;
    lastRenderedFrames = null;
    lastRenderedSampleTime = null;
    updateFpsDisplay("FPS: --");
  };

  const pollWebRtcStats = async () => {
    if (!pc || typeof pc.getStats !== "function") {
      return;
    }

    try {
      const stats = await pc.getStats();
      let measuredFps = null;

      stats.forEach((report) => {
        if (report.type === "inbound-rtp" && report.kind === "video") {
          if (typeof report.framesPerSecond === "number") {
            measuredFps = report.framesPerSecond;
          } else if (typeof report.framesDecoded === "number" && typeof report.timestamp === "number") {
            if (lastInboundVideoStats) {
              const frameDelta = report.framesDecoded - lastInboundVideoStats.framesDecoded;
              const timeDelta = (report.timestamp - lastInboundVideoStats.timestamp) / 1000;
              if (timeDelta > 0) {
                measuredFps = frameDelta / timeDelta;
              }
            }
            lastInboundVideoStats = {
              framesDecoded: report.framesDecoded,
              timestamp: report.timestamp,
            };
          }
        } else if (report.type === "track" && report.kind === "video" && typeof report.framesPerSecond === "number") {
          measuredFps = report.framesPerSecond;
        }
      });

      if ((measuredFps === null || !Number.isFinite(measuredFps)) && videoEl && typeof videoEl.getVideoPlaybackQuality === "function") {
        const quality = videoEl.getVideoPlaybackQuality();
        const now = performance.now();

        if (lastRenderedFrames !== null && lastRenderedSampleTime !== null) {
          const frameDelta = quality.totalVideoFrames - lastRenderedFrames;
          const timeDelta = (now - lastRenderedSampleTime) / 1000;
          if (timeDelta > 0) {
            const estimated = frameDelta / timeDelta;
            if (Number.isFinite(estimated) && estimated >= 0) {
              measuredFps = estimated;
            }
          }
        }

        lastRenderedFrames = quality.totalVideoFrames;
        lastRenderedSampleTime = now;
      }

      if (measuredFps !== null && Number.isFinite(measuredFps)) {
        updateFpsDisplay(`FPS: ${measuredFps.toFixed(1)}`);
      } else {
        updateFpsDisplay("FPS: --");
      }
    } catch (err) {
      console.debug("WebRTC stats polling failed", err);
    }
  };

  const startWebRtcStats = () => {
    if (!pc || typeof pc.getStats !== "function") {
      updateFpsDisplay("FPS: --");
      return;
    }

    stopWebRtcStats();
    pollWebRtcStats();
    webrtcStatsTimer = window.setInterval(pollWebRtcStats, 1000);
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

  // ---- MediaMTX Configuration ----
  const mediamtxUrl = (() => {
    // MediaMTX WebRTC endpoint
    const configured = window.TRAIN_SPOTTER && window.TRAIN_SPOTTER.mediamtxUrl;
    if (configured) return configured;

    const host = window.location.hostname;
    const protocol = window.location.protocol;
    // Default MediaMTX WebRTC port is 8889
    return `${protocol}//${host}:8889/trainspotter/whep`;
  })();

  // ---- MJPEG Fallback ----
  let mjpegWs = null;
  let mjpegReconnectTimer = null;
  let mjpegBackoffMs = 1000;
  let fallbackActive = false;

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

  // ---- MediaMTX WHEP Client ----
  let restartTimeout = null;
  let sessionUrl = null;
  let queuedCandidates = [];
  let retryCount = 0;
  const maxRetries = 5;

  const unquoteCredential = (v) => (
    JSON.parse(`"${v}"`)
  );

  const linkToIceServers = (links) => {
    return (links !== null) ? links.split(', ').map((link) => {
      const m = link.match(/^<(.+?)>; rel="ice-server"(; username="(.*?)"; credential="(.*?)"; credential-type="password")?/i);
      const ret = {
        urls: [m[1]],
      };

      if (m[3] !== undefined) {
        ret.username = unquoteCredential(m[3]);
        ret.credential = unquoteCredential(m[4]);
        ret.credentialType = "password";
      }

      return ret;
    }) : [];
  };

  const parseOffer = (offer) => {
    const ret = {
      iceUfrag: '',
      icePwd: '',
      medias: [],
    };

    for (const line of offer.split('\r\n')) {
      if (line.startsWith('m=')) {
        ret.medias.push(line.slice('m='.length));
      } else if (ret.iceUfrag === '' && line.startsWith('a=ice-ufrag:')) {
        ret.iceUfrag = line.slice('a=ice-ufrag:'.length);
      } else if (ret.icePwd === '' && line.startsWith('a=ice-pwd:')) {
        ret.icePwd = line.slice('a=ice-pwd:'.length);
      }
    }

    return ret;
  };

  const generateSdpFragment = (od, candidates) => {
    const candidatesByMedia = {};
    for (const candidate of candidates) {
      const mid = candidate.sdpMLineIndex;
      if (candidatesByMedia[mid] === undefined) {
        candidatesByMedia[mid] = [];
      }
      candidatesByMedia[mid].push(candidate);
    }

    let frag = 'a=ice-ufrag:' + od.iceUfrag + '\r\n'
      + 'a=ice-pwd:' + od.icePwd + '\r\n';

    let mid = 0;

    for (const media of od.medias) {
      if (candidatesByMedia[mid] !== undefined) {
        frag += 'm=' + media + '\r\n'
          + 'a=mid:' + mid + '\r\n';

        for (const candidate of candidatesByMedia[mid]) {
          frag += 'a=' + candidate.candidate + '\r\n';
        }
      }
      mid++;
    }

    return frag;
  };

  const connect = async () => {
    if (fallbackActive) return;

    stopWebRtcStats();

    setStatus("Connecting to MediaMTX...");
    console.log("MediaMTX: Connecting to", mediamtxUrl);

    pc = new RTCPeerConnection({
      iceServers: [],
      bundlePolicy: 'max-bundle',
    });

    const direction = "sendrecv";
    pc.addTransceiver("video", { direction });
    pc.addTransceiver("audio", { direction });

    pc.ontrack = (evt) => {
      console.log("MediaMTX: Track received", evt.track.kind);
      if (evt.track.kind === 'video' && videoEl) {
        videoEl.srcObject = evt.streams[0];
        videoEl.play().catch(err => {
          console.debug("Video play blocked, will retry after user interaction", err);
        });
        showVideo();
        setStatus("Streaming");
        startWebRtcStats();
        retryCount = 0;
      }
    };

    pc.onicecandidate = (evt) => {
      if (evt.candidate !== null) {
        if (restartTimeout !== null) {
          return;
        }

        if (sessionUrl === null) {
          queuedCandidates.push(evt.candidate);
        } else {
          sendLocalCandidates([evt.candidate]);
        }
      }
    };

    pc.oniceconnectionstatechange = () => {
      console.log("MediaMTX: ICE connection state:", pc.iceConnectionState);
      if (restartTimeout !== null) {
        return;
      }

      if (pc.iceConnectionState === 'failed') {
        console.error("MediaMTX: ICE connection failed");
        scheduleRestart();
      }
    };

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);

    try {
      const res = await fetch(mediamtxUrl, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/sdp',
        },
        body: offer.sdp,
      });

      if (!res.ok) {
        throw new Error(`MediaMTX returned ${res.status}: ${await res.text()}`);
      }

      sessionUrl = new URL(res.headers.get('location'), mediamtxUrl).toString();
      console.log("MediaMTX: Session URL:", sessionUrl);

      pc.setRemoteDescription(new RTCSessionDescription({
        type: 'answer',
        sdp: await res.text(),
      }));

      const iceServers = linkToIceServers(res.headers.get('link'));
      if (iceServers.length > 0) {
        pc.setConfiguration({
          iceServers,
        });
      }

      if (queuedCandidates.length !== 0) {
        sendLocalCandidates(queuedCandidates);
        queuedCandidates = [];
      }

      setStatus("Negotiating connection...");

    } catch (err) {
      console.error("MediaMTX: Connection failed:", err);
      setStatus(`Connection failed: ${err.message}`);
      scheduleRestart();
    }
  };

  const sendLocalCandidates = (candidates) => {
    fetch(sessionUrl, {
      method: 'PATCH',
      headers: {
        'Content-Type': 'application/trickle-ice-sdpfrag',
        'If-Match': '*',
      },
      body: generateSdpFragment(parseOffer(pc.localDescription.sdp), candidates),
    }).then((res) => {
      if (!res.ok) {
        throw new Error(`PATCH failed: ${res.status}`);
      }
    }).catch(err => {
      console.error("MediaMTX: Failed to send ICE candidates:", err);
    });
  };

  const scheduleRestart = () => {
    if (restartTimeout !== null || fallbackActive) {
      return;
    }

    retryCount++;
    if (retryCount >= maxRetries) {
      console.warn("MediaMTX: Max retries reached, switching to MJPEG fallback");
      setStatus("WebRTC retries exhausted. Switching to MJPEG fallback...");
      activateMjpegFallback();
      return;
    }

    if (sessionUrl !== null) {
      fetch(sessionUrl, {
        method: 'DELETE',
      }).catch(err => {
        console.debug("Failed to delete session:", err);
      });
      sessionUrl = null;
    }

    stopWebRtcStats();

    if (pc !== null) {
      pc.close();
      pc = null;
    }

    restartTimeout = setTimeout(() => {
      restartTimeout = null;
      connect();
    }, 2000);

    setStatus(`Connection failed. Retrying in 2s... (${retryCount}/${maxRetries})`);
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
      setStatus("MJPEG stream error. Retrying...");
      scheduleMjpegReconnect();
    };

    mjpegWs.onclose = () => {
      if (!fallbackActive) return;
      setStatus("MJPEG stream closed. Reconnecting...");
      scheduleMjpegReconnect();
    };
  };

  const activateMjpegFallback = () => {
    if (fallbackActive) return;
    console.warn("Activating MJPEG fallback");
    fallbackActive = true;

    if (restartTimeout !== null) {
      clearTimeout(restartTimeout);
      restartTimeout = null;
    }

    if (sessionUrl !== null) {
      fetch(sessionUrl, {
        method: 'DELETE',
      }).catch(() => {});
      sessionUrl = null;
    }

    if (pc !== null) {
      pc.close();
      pc = null;
    }

    stopWebRtcStats();

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
    stopMjpegStream();
    stopWebRtcStats();
    if (sessionUrl !== null) {
      fetch(sessionUrl, {
        method: 'DELETE',
      }).catch(() => {});
    }
    if (pc !== null) {
      pc.close();
    }
  });

  if (videoEl) {
    videoEl.addEventListener("loadedmetadata", () => {
      console.log("Video metadata loaded:", videoEl.videoWidth, videoEl.videoHeight);
    });
    videoEl.addEventListener("loadeddata", () => {
      console.log("First frame rendered");
    });
    videoEl.addEventListener("error", (err) => {
      console.error("Video element error:", err);
    });
  }

  // Start streaming
  if (videoEl) showVideo();
  connect();
})();
