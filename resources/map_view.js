// 1. Zmienne filtrujące i definicje reguł ZAWSZE na samej górze
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
    if (!map || !map.getLayer('tram-tracks')) return;
    const checkbox = document.getElementById('filter-trams');
    const isVisible = checkbox ? checkbox.checked : true;
    map.setLayoutProperty('tram-tracks', 'visibility', isVisible ? 'visible' : 'none');
}

function toggleMobileFilters() {
    const legend = document.getElementById('legend');
    if (legend) {
        legend.classList.toggle('active');
    }
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

function initMap() {
    const container = document.getElementById('map');
    if (!container) {
        console.error("Brak kontenera #map w drzewie DOM!");
        return;
    }

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

        // 2. Torowiska tramwajowe
        map.addSource('tram-network', {
            type: 'geojson',
            data: '/network/tram'
        });

        map.addLayer({
            id: 'tram-tracks',
            type: 'line',
            source: 'tram-network',
            layout: {
                'line-join': 'round',
                'line-cap': 'round'
            },
            paint: {
                'line-color': '#3b0141',
                'line-width': 2.5,
                'line-opacity': 0.85
            }
        });

        // 3. Stacje Veturilo
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

        const initialData = await fetchStations();
        setInterval(fetchStations, 30000);

        map.on('click', 'stations-point', (e) => {
            const p = e.features[0].properties;
            new maplibregl.Popup({ offset: 8 })
                .setLngLat(e.lngLat)
                .setHTML(createPopupHTML(p.id, p.name, p.free_bikes, p.empty_slots))
                .addTo(map);
        });

        map.on('mouseenter', 'stations-point', () => { map.getCanvas().style.cursor = 'pointer'; });
        map.on('mouseleave', 'stations-point', () => { map.getCanvas().style.cursor = ''; });

        // Zamknięcie wysuwanego menu na mobile po dotknięciu tła mapy
        map.on('click', (e) => {
            const features = map.queryRenderedFeatures(e.point, { layers: ['stations-point'] });
            if (!features.length) {
                const legend = document.getElementById('legend');
                if (legend && legend.classList.contains('active')) {
                    legend.classList.remove('active');
                }
            }
        });

        const params = new URLSearchParams(window.location.search);
        const lat = parseFloat(params.get('lat'));
        const lng = parseFloat(params.get('lng'));
        const targetId = params.get('id');
        const targetName = decodeURIComponent(params.get('name') || 'Stacja');

        if (!isNaN(lat) && !isNaN(lng)) {
            map.flyTo({ center: [lng, lat], zoom: 16, essential: true });
            let freeBikes = undefined, emptySlots = undefined;
            if (initialData && initialData.features) {
                const match = initialData.features.find(f => f.properties.id === targetId);
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