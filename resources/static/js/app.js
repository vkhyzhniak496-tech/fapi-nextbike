// ==========================================
// WARSZAWA MULTIMODAL GIS - JEDNOLITY KONTROLER MAPY
// ==========================================

const FILTER_RULES = {
    ddr: ['==', ['get', 'infra_type'], 'ddr'],
    lane: ['==', ['get', 'infra_type'], 'lane'],
    woonerf: ['==', ['get', 'infra_type'], 'woonerf'],
    tempo30: ['==', ['get', 'infra_type'], 'tempo30'],
};

function formatTime(val) {
    if (!val) return "Brak danych";
    const date = new Date(val);
    return isNaN(date.getTime()) 
        ? "Aktualne" 
        : date.toLocaleTimeString('pl-PL', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function createBikePopupHTML(id, name, freeBikes, emptySlots) {
    const encodedName = encodeURIComponent(name);
    const cacheKey = Math.floor(Date.now() / 60000);
    return `
        <div class="popup-title">${name}</div>
        <div style="margin: 6px 0;">
            <span class="metric-badge">🚲 Rowery: <b>${freeBikes ?? '?'}</b></span>
            <span class="metric-badge">🅿️ Stojaki: <b>${emptySlots ?? '?'}</b></span>
        </div>
        <div class="station-chart-container">
            <img 
                src="/analytics/station/${id}/chart.png?name=${encodedName}&t=${cacheKey}" 
                class="station-chart-img" 
                alt="Ładowanie wykresu..." 
                onerror="this.parentElement.style.display='none'"
            />
        </div>
    `;
}

// --- 1. REJESTRACJA WARSTW MAPLIBRE ---

function setupLayers(map) {
    // A. Ścieżki rowerowe
    map.addSource('safe-cycleways', { type: 'geojson', data: '/network/safe-cycleways' });
    map.addLayer({
        id: 'cycleways-lines',
        type: 'line',
        source: 'safe-cycleways',
        layout: { 'line-join': 'round', 'line-cap': 'round' },
        paint: {
            'line-color': [
                'match', ['get', 'infra_type'],
                'ddr', '#27ae60',
                'lane', '#f39c12',
                'woonerf', '#2980b9',
                'tempo30', '#3498db',
                '#3498db'
            ],
            'line-width': 3,
            'line-opacity': 0.85,
        },
    });

    // B. Torowiska tramwajowe
    map.addSource('tram-network', { type: 'geojson', data: '/network/tram' });
    map.addLayer({
        id: 'tram-tracks',
        type: 'line',
        source: 'tram-network',
        layout: { 'line-join': 'round', 'line-cap': 'round' },
        paint: {
            'line-color': '#3b0141',
            'line-width': 2.5,
            'line-opacity': 0.85,
        },
    });

    // C. Perony tramwajowe
    map.addSource('tram-platforms', { type: 'geojson', data: '/network/tram/platforms' });
    map.addLayer({
        id: 'tram-platforms-circle',
        type: 'circle',
        source: 'tram-platforms',
        paint: {
            'circle-radius': 4,
            'circle-color': '#ffffff',
            'circle-stroke-width': 2,
            'circle-stroke-color': '#8e44ad',
        },
    });
    map.addLayer({
        id: 'tram-platforms-label',
        type: 'symbol',
        source: 'tram-platforms',
        minzoom: 13.5,
        layout: {
            'text-field': ['get', 'name'],
            'text-size': 11,
            'text-offset': [0, 1.2],
            'text-anchor': 'top',
            'text-max-width': 10,
        },
        paint: {
            'text-color': '#2c3e50',
            'text-halo-color': '#ffffff',
            'text-halo-width': 1.5,
        },
    });

    // D. Telemetria tramwajów na żywo
    map.addSource('tram-live-source', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] }
    });
    map.addLayer({
        id: 'tram-live-layer',
        type: 'circle',
        source: 'tram-live-source',
        paint: {
            'circle-radius': 7,
            'circle-color': '#9b59b6',
            'circle-stroke-width': 2,
            'circle-stroke-color': '#ffffff'
        }
    });
    map.addLayer({
        id: 'tram-live-labels',
        type: 'symbol',
        source: 'tram-live-source',
        layout: {
            'text-field': ['get', 'line'],
            'text-size': 11,
            'text-font': ['Open Sans Bold', 'Arial Unicode MS Bold'],
            'text-offset': [0, -1.2],
            'text-anchor': 'bottom',
            'text-allow-overlap': true
        },
        paint: {
            'text-color': '#1e293b',
            'text-halo-color': '#ffffff',
            'text-halo-width': 2.5
        }
    });

    // E. Stacje Veturilo
    map.addSource('veturilo-stations', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] }
    });
    map.addLayer({
        id: 'stations-point',
        type: 'circle',
        source: 'veturilo-stations',
        paint: {
            'circle-radius': [
                'interpolate', ['linear'], ['get', 'free_bikes'],
                0, 4,
                5, 7,
                15, 12
            ],
            'circle-color': [
                'case',
                ['==', ['get', 'free_bikes'], 0], '#e74c3c',
                '#2ecc71'
            ],
            'circle-stroke-width': 1.5,
            'circle-stroke-color': '#ffffff'
        }
    });

    // Obsługa kliknięcia w stację Veturilo
    map.on('click', 'stations-point', (e) => {
        const p = e.features[0].properties;
        new maplibregl.Popup({ offset: 8 })
            .setLngLat(e.lngLat)
            .setHTML(createBikePopupHTML(p.id, p.name, p.free_bikes, p.empty_slots))
            .addTo(map);
    });

    map.on('mouseenter', 'stations-point', () => { map.getCanvas().style.cursor = 'pointer'; });
    map.on('mouseleave', 'stations-point', () => { map.getCanvas().style.cursor = ''; });
}

// --- 2. POBIERANIE DANYCH ---

async function fetchStations(map) {
    try {
        const res = await fetch('/bikes/citybikes/warsaw');
        const data = await res.json();
        if (data.last_update) {
            const label = document.getElementById('last-update-label');
            if (label) label.innerText = formatTime(data.last_update);
        }
        if (map && map.getSource('veturilo-stations')) {
            map.getSource('veturilo-stations').setData(data);
        }
        return data;
    } catch (err) {
        console.error("Błąd pobierania stacji Veturilo:", err);
    }
}

async function fetchLiveTrams(map) {
    try {
        const res = await fetch('/network/tram/live');
        if (res.ok) {
            const data = await res.json();
            if (map && map.getSource('tram-live-source')) {
                map.getSource('tram-live-source').setData(data);
            }
        }
    } catch (err) {
        console.warn("Błąd pobierania pozycji tramwajów:", err);
    }
}

// --- 3. FILTRY I INTERAKCJA UI ---

function updateCyclewaysFilter(map) {
    if (!map || !map.getLayer('cycleways-lines')) return;

    const activeRules = [];
    if (document.getElementById('filter-ddr')?.checked) activeRules.push(FILTER_RULES.ddr);
    if (document.getElementById('filter-lane')?.checked) activeRules.push(FILTER_RULES.lane);
    if (document.getElementById('filter-woonerf')?.checked) activeRules.push(FILTER_RULES.woonerf);
    if (document.getElementById('filter-tempo30')?.checked) activeRules.push(FILTER_RULES.tempo30);

    if (activeRules.length === 0) {
        map.setFilter('cycleways-lines', ['==', ['get', 'highway'], '__none__']);
    } else {
        map.setFilter('cycleways-lines', ['any', ...activeRules]);
    }
}

function toggleTramTracks(map) {
    if (!map) return;
    const isVisible = document.getElementById('filter-trams')?.checked ?? true;
    const state = isVisible ? 'visible' : 'none';

    const tramLayers = [
        'tram-tracks',
        'tram-platforms-circle',
        'tram-platforms-label',
        'tram-live-layer',
        'tram-live-labels'
    ];

    tramLayers.forEach(id => {
        if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', state);
    });
}

function handleUrlTarget(map, initialData) {
    const params = new URLSearchParams(window.location.search);
    const lat = parseFloat(params.get('lat'));
    const lng = parseFloat(params.get('lng'));
    const targetId = params.get('station') || params.get('id');
    const targetName = decodeURIComponent(params.get('name') || 'Stacja');

    if (!isNaN(lat) && !isNaN(lng)) {
        map.flyTo({ center: [lng, lat], zoom: 16.5, speed: 1.6, essential: true });

        if (targetId) {
            let freeBikes, emptySlots;
            if (initialData?.features) {
                const match = initialData.features.find(item => item.properties.id === targetId);
                if (match) {
                    freeBikes = match.properties.free_bikes;
                    emptySlots = match.properties.empty_slots;
                }
            }
            new maplibregl.Popup({ offset: 8 })
                .setLngLat([lng, lat])
                .setHTML(createBikePopupHTML(targetId, targetName, freeBikes, emptySlots))
                .addTo(map);
        }
    }
}

// --- 4. START APLIKACJI ---

document.addEventListener('DOMContentLoaded', () => {
    const map = new maplibregl.Map({
        container: 'map',
        style: 'https://basemaps.cartocdn.com/gl/positron-gl-style/style.json',
        center: [21.012, 52.230],
        zoom: 12.5
    });

    map.addControl(new maplibregl.NavigationControl(), 'top-right');
    map.addControl(new maplibregl.GeolocateControl({
        positionOptions: { enableHighAccuracy: true },
        trackUserLocation: true,
        showUserLocation: true
    }), 'top-right');

    // UI zdarzenia
    const toggleMobile = () => document.getElementById('legend')?.classList.toggle('active');
    document.getElementById('btn-toggle-filters')?.addEventListener('click', toggleMobile);
    document.getElementById('btn-close-legend')?.addEventListener('click', toggleMobile);

    ['filter-ddr', 'filter-lane', 'filter-woonerf', 'filter-tempo30'].forEach(id => {
        document.getElementById(id)?.addEventListener('change', () => updateCyclewaysFilter(map));
    });
    document.getElementById('filter-trams')?.addEventListener('change', () => toggleTramTracks(map));

    map.on('load', async () => {
        setupLayers(map);

        // Pierwsze pobranie danych i obsługa linku z Leaderboardu
        const initialStations = await fetchStations(map);
        handleUrlTarget(map, initialStations);

        // Polling w tle: Veturilo co 30s, tramwaje co 10s
        setInterval(() => fetchStations(map), 30000);

        await fetchLiveTrams(map);
        setInterval(() => fetchLiveTrams(map), 10000);
    });
});