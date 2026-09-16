export function setupTramLiveLayers(map) {
    // 1. Tramwaje na żywo
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

    // 2. Zadanie 3: Warstwa analityczna czasów wymiany pasażerskiej (Dwells)
    map.addSource('tram-dwells-source', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] }
    });

    map.addLayer({
        id: 'tram-dwell-points',
        type: 'circle',
        source: 'tram-dwells-source',
        layout: { 'visibility': 'none' }, // Domyślnie schowane do aktywacji filtrem
        paint: {
            'circle-radius': [
                'interpolate', ['linear'], ['get', 'avg_dwell_sec'],
                10, 5,
                30, 10,
                60, 16
            ],
            'circle-color': [
                'interpolate', ['linear'], ['get', 'avg_dwell_sec'],
                15, '#2ecc71',
                30, '#f39c12',
                50, '#e74c3c'
            ],
            'circle-opacity': 0.8,
            'circle-stroke-width': 1.5,
            'circle-stroke-color': '#ffffff'
        }
    });

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
}

export async function fetchLiveTrams(map) {
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