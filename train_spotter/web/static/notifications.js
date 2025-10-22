/**
 * Real-time notification system using Server-Sent Events and Bootstrap Toasts
 */
(() => {
  let eventSource = null;
  let reconnectAttempts = 0;
  const maxReconnectAttempts = 5;
  const reconnectDelay = 3000; // 3 seconds

  // Helper to show Bootstrap toast
  const showToast = (title, message, type = 'info') => {
    const container = document.getElementById('toast-container');
    if (!container) return;

    // Map type to Bootstrap classes
    const iconMap = {
      info: 'bi-info-circle-fill',
      success: 'bi-check-circle-fill',
      warning: 'bi-exclamation-triangle-fill',
      danger: 'bi-x-circle-fill',
      train: 'bi-train-front-fill',
      vehicle: 'bi-car-front-fill',
    };

    const bgMap = {
      info: 'bg-info',
      success: 'bg-success',
      warning: 'bg-warning',
      danger: 'bg-danger',
      train: 'bg-primary',
      vehicle: 'bg-success',
    };

    const icon = iconMap[type] || iconMap.info;
    const bg = bgMap[type] || bgMap.info;

    // Create toast element
    const toastId = `toast-${Date.now()}`;
    const toastHTML = `
      <div id="${toastId}" class="toast" role="alert" aria-live="assertive" aria-atomic="true">
        <div class="toast-header ${bg} text-white">
          <i class="${icon} me-2"></i>
          <strong class="me-auto">${title}</strong>
          <small>Just now</small>
          <button type="button" class="btn-close btn-close-white" data-bs-dismiss="toast" aria-label="Close"></button>
        </div>
        <div class="toast-body">
          ${message}
        </div>
      </div>
    `;

    // Insert toast
    container.insertAdjacentHTML('beforeend', toastHTML);

    // Initialize and show toast
    const toastElement = document.getElementById(toastId);
    const toast = new bootstrap.Toast(toastElement, {
      autohide: true,
      delay: 5000, // 5 seconds
    });

    toast.show();

    // Remove from DOM after hidden
    toastElement.addEventListener('hidden.bs.toast', () => {
      toastElement.remove();
    });

    // Also try browser notification if permitted
    if (type === 'train' && 'Notification' in window && Notification.permission === 'granted') {
      new Notification(title, {
        body: message,
        icon: '/static/train-icon.png', // Optional: add icon
      });
    }
  };

  // Connect to SSE stream
  const connectEventStream = () => {
    if (eventSource) {
      try {
        eventSource.close();
      } catch (e) {
        console.debug('Error closing previous event source:', e);
      }
    }

    try {
      eventSource = new EventSource('/api/events/stream');
    } catch (err) {
      console.error('Failed to create EventSource:', err);
      return;
    }

    eventSource.onopen = () => {
      console.log('Event stream connected');
      reconnectAttempts = 0;
    };

    eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);

        // Handle different event types
        if (data.type === 'train_started') {
          showToast(
            'Train Detected',
            `Train ${data.payload.train_id || 'Unknown'} detected entering the scene`,
            'train'
          );
        } else if (data.type === 'train_ended') {
          const duration = data.payload.duration
            ? ` (${data.payload.duration.toFixed(1)}s)`
            : '';
          showToast(
            'Train Passed',
            `Train ${data.payload.train_id || 'Unknown'} has left the scene${duration}`,
            'success'
          );
        } else if (data.type === 'vehicle_event') {
          const label = data.payload.class_label || 'Vehicle';
          const lane = data.payload.lane_id || '?';
          showToast(
            'Vehicle Detected',
            `${label.charAt(0).toUpperCase() + label.slice(1)} detected in Lane ${lane}`,
            'vehicle'
          );
        } else if (data.type === 'connected') {
          console.log('Event stream: ', data.message);
        }
      } catch (err) {
        console.warn('Failed to parse event data:', err);
      }
    };

    eventSource.onerror = (err) => {
      console.error('Event stream error:', err);
      eventSource.close();

      // Attempt reconnection
      if (reconnectAttempts < maxReconnectAttempts) {
        reconnectAttempts++;
        console.log(
          `Reconnecting event stream (attempt ${reconnectAttempts}/${maxReconnectAttempts})...`
        );
        setTimeout(connectEventStream, reconnectDelay);
      } else {
        showToast(
          'Connection Lost',
          'Real-time notifications unavailable. Refresh the page to reconnect.',
          'warning'
        );
      }
    };
  };

  // Request notification permission
  const requestNotificationPermission = () => {
    if ('Notification' in window && Notification.permission === 'default') {
      Notification.requestPermission().then((permission) => {
        if (permission === 'granted') {
          console.log('Browser notifications enabled');
        }
      });
    }
  };

  // Initialize
  const init = () => {
    // Request notification permission on user interaction
    document.addEventListener(
      'click',
      () => {
        requestNotificationPermission();
      },
      { once: true }
    );

    // Connect to event stream after a short delay to not block page load
    setTimeout(() => {
      connectEventStream();
    }, 1000);
  };

  // Cleanup on page unload
  window.addEventListener('beforeunload', () => {
    if (eventSource) {
      eventSource.close();
    }
  });

  // Start when DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Expose for debugging
  window.trainSpotterNotifications = {
    showToast,
    reconnect: connectEventStream,
  };
})();
