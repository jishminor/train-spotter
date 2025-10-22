/**
 * Dashboard.js - Chart.js-powered analytics dashboard for Train Spotter
 */
(() => {
  // Chart instances
  let activityChart = null;
  let vehicleTypeChart = null;
  let laneChart = null;
  let hourlyChart = null;

  // Common chart colors
  const colors = {
    primary: '#0f5c8c',
    success: '#28a745',
    warning: '#ffc107',
    danger: '#dc3545',
    info: '#17a2b8',
    secondary: '#6c757d',
  };

  // Initialize all charts
  const initCharts = () => {
    // Activity Over Time (Line Chart)
    const activityCtx = document.getElementById('activity-chart');
    if (activityCtx) {
      activityChart = new Chart(activityCtx, {
        type: 'line',
        data: {
          labels: [],
          datasets: [
            {
              label: 'Trains',
              data: [],
              borderColor: colors.primary,
              backgroundColor: colors.primary + '20',
              tension: 0.4,
              fill: true,
            },
            {
              label: 'Vehicles',
              data: [],
              borderColor: colors.success,
              backgroundColor: colors.success + '20',
              tension: 0.4,
              fill: true,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: {
              position: 'top',
            },
            title: {
              display: false,
            },
          },
          scales: {
            y: {
              beginAtZero: true,
              ticks: {
                precision: 0,
              },
            },
          },
        },
      });
    }

    // Vehicle Type Distribution (Pie Chart)
    const vehicleTypeCtx = document.getElementById('vehicle-type-chart');
    if (vehicleTypeCtx) {
      vehicleTypeChart = new Chart(vehicleTypeCtx, {
        type: 'doughnut',
        data: {
          labels: ['Cars', 'Trucks', 'Other'],
          datasets: [
            {
              data: [0, 0, 0],
              backgroundColor: [colors.success, colors.warning, colors.secondary],
              borderWidth: 2,
              borderColor: '#fff',
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: {
              position: 'bottom',
            },
          },
        },
      });
    }

    // Lane Distribution (Bar Chart)
    const laneCtx = document.getElementById('lane-chart');
    if (laneCtx) {
      laneChart = new Chart(laneCtx, {
        type: 'bar',
        data: {
          labels: [],
          datasets: [
            {
              label: 'Vehicles per Lane',
              data: [],
              backgroundColor: colors.info,
              borderColor: colors.info,
              borderWidth: 1,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: {
              display: false,
            },
          },
          scales: {
            y: {
              beginAtZero: true,
              ticks: {
                precision: 0,
              },
            },
          },
        },
      });
    }

    // Hourly Traffic (Bar Chart)
    const hourlyCtx = document.getElementById('hourly-chart');
    if (hourlyCtx) {
      hourlyChart = new Chart(hourlyCtx, {
        type: 'bar',
        data: {
          labels: Array.from({ length: 24 }, (_, i) => `${i}:00`),
          datasets: [
            {
              label: 'Vehicles',
              data: new Array(24).fill(0),
              backgroundColor: colors.success,
              borderColor: colors.success,
              borderWidth: 1,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: {
              display: false,
            },
          },
          scales: {
            y: {
              beginAtZero: true,
              ticks: {
                precision: 0,
              },
            },
          },
        },
      });
    }
  };

  // Update stats from API
  const updateStats = async () => {
    const loadingSpinner = document.getElementById('loading-spinner');
    const lastUpdateEl = document.getElementById('last-update');

    try {
      if (loadingSpinner) loadingSpinner.classList.remove('d-none');

      const response = await fetch('/api/stats');
      if (!response.ok) {
        console.error('Failed to fetch stats:', response.status);
        return;
      }

      const data = await response.json();

      // Update summary cards
      updateElement('total-trains', data.total_trains || 0);
      updateElement('total-vehicles', data.total_vehicles || 0);
      updateElement('total-trucks', data.total_trucks || 0);
      updateElement('avg-duration', data.avg_duration ? data.avg_duration.toFixed(1) : '-');

      // Update charts
      updateActivityChart(data.activity_over_time || []);
      updateVehicleTypeChart(data.vehicle_types || {});
      updateLaneChart(data.lane_distribution || {});
      updateHourlyChart(data.hourly_traffic || []);

      // Update timestamp
      if (lastUpdateEl) {
        const now = new Date();
        lastUpdateEl.textContent = now.toLocaleTimeString();
      }
    } catch (err) {
      console.error('Error fetching dashboard stats:', err);
    } finally {
      if (loadingSpinner) loadingSpinner.classList.add('d-none');
    }
  };

  // Helper to update element text
  const updateElement = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  };

  // Update activity chart
  const updateActivityChart = (data) => {
    if (!activityChart || !data.length) return;

    const labels = data.map((d) => d.label);
    const trainData = data.map((d) => d.trains || 0);
    const vehicleData = data.map((d) => d.vehicles || 0);

    activityChart.data.labels = labels;
    activityChart.data.datasets[0].data = trainData;
    activityChart.data.datasets[1].data = vehicleData;
    activityChart.update('none'); // Skip animation for real-time updates
  };

  // Update vehicle type chart
  const updateVehicleTypeChart = (data) => {
    if (!vehicleTypeChart) return;

    const cars = data.car || data.cars || 0;
    const trucks = data.truck || data.trucks || 0;
    const other = data.other || 0;

    vehicleTypeChart.data.datasets[0].data = [cars, trucks, other];
    vehicleTypeChart.update('none');
  };

  // Update lane distribution chart
  const updateLaneChart = (data) => {
    if (!laneChart) return;

    const lanes = Object.keys(data).sort();
    const counts = lanes.map((lane) => data[lane]);

    laneChart.data.labels = lanes.map((lane) => `Lane ${lane}`);
    laneChart.data.datasets[0].data = counts;
    laneChart.update('none');
  };

  // Update hourly traffic chart
  const updateHourlyChart = (data) => {
    if (!hourlyChart) return;

    // data is array of 24 hourly counts
    if (Array.isArray(data) && data.length === 24) {
      hourlyChart.data.datasets[0].data = data;
      hourlyChart.update('none');
    }
  };

  // Auto-refresh
  let refreshInterval = null;
  const startAutoRefresh = () => {
    updateStats(); // Initial update
    refreshInterval = setInterval(updateStats, 5000); // Refresh every 5 seconds
  };

  const stopAutoRefresh = () => {
    if (refreshInterval) {
      clearInterval(refreshInterval);
      refreshInterval = null;
    }
  };

  // Initialize when DOM is ready
  const init = () => {
    initCharts();
    startAutoRefresh();
  };

  // Cleanup on page unload
  window.addEventListener('beforeunload', () => {
    stopAutoRefresh();
  });

  // Start
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
