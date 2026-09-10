const IS_DDR = ['==', ['get', 'highway'], 'cycleway'];
const HAS_LANE = [
    'any',
    ['has', 'cycleway'],
    ['has', 'cycleway:left'],
    ['has', 'cycleway:right'],
    ['has', 'cycleway:both'],
    ['==', ['get', 'oneway:bicycle'], 'no']
];
const IS_WOONERF = ['==', ['get', 'highway'], 'living_street'];

const FILTER_RULES = {
    ddr: IS_DDR,
    lane: ['all', ['!', IS_DDR], HAS_LANE],
    woonerf: ['all', ['!', IS_DDR], ['!', HAS_LANE], IS_WOONERF],
    tempo30: ['all', ['!', IS_DDR], ['!', HAS_LANE], ['!', IS_WOONERF]]
};

let map = null;

function formatTime(val) {
    if (!val) return "Brak danych";
    const date = new Date(val);
    return isNaN(date.getTime()) ? "Aktualne" : date.toLocaleTimeString('pl-PL', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function updateCyclewaysFilter() {
    if (!map || !map.getLayer('cycleways-lines')) return;

    const activeRules = [];
    const ddrEl = document.getElementById('filter-ddr');
    const laneEl = document.getElementById('filter-lane');
    const woonerfEl = document.getElementById('filter-woonerf');
    const tempo30El = document.getElementById('filter-tempo30');

    if (ddrEl && ddrEl.checked) activeRules.push(FILTER_RULES.ddr);
    if (laneEl && laneEl.checked) activeRules.push(FILTER_RULES.lane);
    if (woonerfEl && woonerfEl.checked) activeRules.push(FILTER_RULES.woonerf);
    if (tempo30El && tempo30El.checked) activeRules.push(FILTER_RULES.tempo30);

    if (activeRules.length === 0) {
        map.setFilter('cycleways-lines', ['==', ['get', 'highway'], '__none__']);
    } else {
        map.setFilter('cycleways-lines', ['any', ...activeRules]);
    }
}

function toggleTramTracks() {
    if (!map) return;
    const checkbox = document.getElementById('filter-trams');
    const isVisible = checkbox ? checkbox.checked : true;
    const state = isVisible ? 'visible' : 'none';

    // Tory, perony, tramwaje i ich etykiety znikają razem
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

function toggleMobileFilters() {
    const legend = document.getElementById('legend');
    if (legend) legend.classList.toggle('active');
}

function createPopupHTML(id, name, freeBikes, emptySlots) {
    const encodedName = encodeURIComponent(name);
    const cacheKey = Math.floor(Date.now() / 60000);
    return `
        <div class="popup-title">${name}</div>
        <div>
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

async function fetchStations() {
    try {
        const res = await fetch('/bikes/citybikes/warsaw');
        const data = await res.json();
        if (data.last_update) {
            const label = document.getElementById('last-update-label');
            if (label) label.innerText = formatTime(data.last_update);
        }
        if (map) {
            const source = map.getSource('veturilo-stations');
            if (source) source.setData(data);
        }
        return data;
    } catch (err) {
        console.error("Błąd stacji:", err);
    }
}

async function fetchLiveTrams() {
    try {
        const res = await fetch('/network/tram/live');
        if (res.ok) {
            const data = await res.json();
            if (map) {
                const source = map.getSource('tram-live-source');
                if (source) source.setData(data);
            }
        }
    } catch (err) {
        console.warn("Błąd telemetrii tramwajów:", err);
    }
}

function initMap() {
    const container = document.getElementById('map');
    if (!container) return;

    map = new maplibregl.Map({
        container: 'map',
        style: 'https://basemaps.cartocdn.com/gl/positron-gl-style/style.json',
        center: [21.012, 52.230],
        zoom: 13
    });

    map.addControl(new maplibregl.NavigationControl(), 'top-right');
    map.addControl(new maplibregl.GeolocateControl({
        positionOptions: { enableHighAccuracy: true },
        trackUserLocation: true,
        showUserLocation: true
    }), 'top-right');

    map.on('load', async () => {
        // 1. Ścieżki rowerowe
        map.addSource('safe-cycleways', {
            type: 'geojson',
            data: '/network/safe-cycleways'
        });

        map.addLayer({
            id: 'cycleways-lines',
            type: 'line',
            source: 'safe-cycleways',
            layout: { 'line-join': 'round', 'line-cap': 'round' },
            paint: {
                'line-color': [
                    'case',
                    FILTER_RULES.ddr, '#27ae60',
                    FILTER_RULES.lane, '#f39c12',
                    FILTER_RULES.woonerf, '#2980b9',
                    '#3498db'
                ],
                'line-width': 3,
                'line-opacity': 0.85
            }
        });

        // 2. Torowiska
        map.addSource('tram-network', {
            type: 'geojson',
            data: '/network/tram'
        });

        map.addLayer({
            id: 'tram-tracks',
            type: 'line',
            source: 'tram-network',
            layout: { 'line-join': 'round', 'line-cap': 'round' },
            paint: {
                'line-color': '#3b0141',
                'line-width': 2.5,
                'line-opacity': 0.85
            }
        });

        // 2a. Przystanki
        map.addSource('tram-platforms', {
            type: 'geojson',
            data: '/network/tram/platforms'
        });

        map.addLayer({
            id: 'tram-platforms-circle',
            type: 'circle',
            source: 'tram-platforms',
            paint: {
                'circle-radius': 4,
                'circle-color': '#ffffff',
                'circle-stroke-width': 2,
                'circle-stroke-color': '#8e44ad'
            }
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
                'text-max-width': 10
            },
            paint: {
                'text-color': '#2c3e50',
                'text-halo-color': '#ffffff',
                'text-halo-width': 1.5
            }
        });

        // 2b. Tramwaje na żywo - Kółka
        map.addSource('tram-live-source', {
            type: 'geojson',
            data: { type: 'FeatureCollection', features: [] }
        });

        map.addLayer({
            id: 'tram-live-layer',
            type: 'circle',
            source: 'tram-live-source',
            paint: {
                'circle-radius': 6.5,
                'circle-color': '#e056fd',
                'circle-stroke-width': 1.8,
                'circle-stroke-color': '#ffffff'
            }
        });

        // 2c. Tramwaje na żywo - Numery linii
        map.addLayer({
            id: 'tram-live-labels',
            type: 'symbol',
            source: 'tram-live-source',
            layout: {
                'text-field': ['get', 'line'],
                'text-size': 11,
                'text-offset': [0, -1.3],
                'text-anchor': 'bottom',
                'text-allow-overlap': true
            },
            paint: {
                'text-color': '#2c3e50',
                'text-halo-color': '#ffffff',
                'text-halo-width': 2
            }
        });

        // 3. Stacje rowerowe
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

        // Start pobierania danych
        const initialData = await fetchStations();
        setInterval(fetchStations, 30000);

        await fetchLiveTrams();
        setInterval(fetchLiveTrams, 10000);

        // Klik w tramwaj -> Dymek z prędkością
        map.on('click', 'tram-live-layer', (e) => {
            const p = e.features[0].properties;
            const coords = e.features[0].geometry.coordinates.slice();
            const speed = p.speed_kmh !== undefined ? Math.round(p.speed_kmh) : 0;
            const vNum = p.vehicle_number || p.id || '---';

            new maplibregl.Popup({ offset: 8 })
                .setLngLat(coords)
                .setHTML(`
                    <div style="font-size: 14px; font-weight: 700; color: #8e44ad; margin-bottom: 4px;">
                        🚋 Linia ${p.line} (#${vNum})
                    </div>
                    <div style="font-size: 13px;">⚡ Prędkość: <b>${speed} km/h</b></div>
                    <div style="font-size: 12px; color: #64748b;">Brygada: <b>${p.brigade || '-'}</b></div>
                    <div style="font-size: 11px; color: #94a3b8; margin-top: 4px;">GPS: ${p.time || ''}</div>
                `)
                .addTo(map);
        });
        map.on('mouseenter', 'tram-live-layer', () => { map.getCanvas().style.cursor = 'pointer'; });
        map.on('mouseleave', 'tram-live-layer', () => { map.getCanvas().style.cursor = ''; });

        // Klik w stację rowerową
        map.on('click', 'stations-point', (e) => {
            const p = e.features[0].properties;
            new maplibregl.Popup({ offset: 8 })
                .setLngLat(e.lngLat)
                .setHTML(createPopupHTML(p.id, p.name, p.free_bikes, p.empty_slots))
                .addTo(map);
        });
        map.on('mouseenter', 'stations-point', () => { map.getCanvas().style.cursor = 'pointer'; });
        map.on('mouseleave', 'stations-point', () => { map.getCanvas().style.cursor = ''; });

        // Mobile popup zamykany tłem
        map.on('click', (e) => {
            const f = map.queryRenderedFeatures(e.point, { layers: ['stations-point', 'tram-live-layer'] });
            if (!f.length) {
                const legend = document.getElementById('legend');
                if (legend && legend.classList.contains('active')) legend.classList.remove('active');
            }
        });

        // Teleport do stacji z URL
        const params = new URLSearchParams(window.location.search);
        const lat = parseFloat(params.get('lat'));
        const lng = parseFloat(params.get('lng'));
        const targetId = params.get('id');
        const targetName = decodeURIComponent(params.get('name') || 'Stacja');

        if (!isNaN(lat) && !isNaN(lng)) {
            map.flyTo({ center: [lng, lat], zoom: 16, essential: true });
            let freeBikes = undefined, emptySlots = undefined;
            if (initialData && initialData.features) {
                const match = initialData.features.find(item => item.properties.id === targetId);
                if (match) {
                    freeBikes = match.properties.free_bikes;
                    emptySlots = match.properties.empty_slots;
                }
            }
            new maplibregl.Popup({ offset: 8 })
                .setLngLat([lng, lat])
                .setHTML(createPopupHTML(targetId, targetName, freeBikes, emptySlots))
                .addTo(map);
        }
    });
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initMap);
} else {
    initMap();
}