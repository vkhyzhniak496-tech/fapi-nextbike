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

async function initBikesLayer(map) {
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

    // Obsługa przekierowania z leaderboarda z parametrami URL
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
}