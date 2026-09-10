let tramFetchInterval = null;

async function fetchLiveTrams(map) {
    try {
        const res = await fetch('/network/tram/live');
        if (!res.ok) return;
        const data = await res.json();
        
        const source = map.getSource('tram-live-source');
        if (source) {
            source.setData(data);
        }
    } catch (err) {
        console.warn("Błąd telemetrii tramwajów:", err);
    }
}

async function initTramLiveLayer(map) {
    const tramCheckbox = document.getElementById('filter-trams');
    const initialVisibility = (tramCheckbox && !tramCheckbox.checked) ? 'none' : 'visible';

    // Źródło tramwajów
    map.addSource('tram-live-source', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] }
    });

    // 1. Kropki tramwajów
    map.addLayer({
        id: 'tram-live-layer',
        type: 'circle',
        source: 'tram-live-source',
        layout: {
            'visibility': initialVisibility
        },
        paint: {
            'circle-radius': 6.5,
            'circle-color': '#e056fd',
            'circle-stroke-width': 1.8,
            'circle-stroke-color': '#ffffff'
        }
    });

    // 2. Numery linii nad tramwajami
    map.addLayer({
        id: 'tram-live-labels',
        type: 'symbol',
        source: 'tram-live-source',
        layout: {
            'visibility': initialVisibility,
            'text-field': ['get', 'line'],
            'text-size': 11,
            'text-offset': [0, -1.3],
            'text-anchor': 'bottom',
            'text-allow-overlap': true
        },
        paint: {
            'text-color': '#2c3e50',
            'text-halo-color': '#ffffff',
            'text-halo-width': 1.8
        }
    });

    // Popup z prędkością i danymi składu
    map.on('click', 'tram-live-layer', (e) => {
        const props = e.features[0].properties;
        const coords = e.features[0].geometry.coordinates.slice();
        
        const speed = props.speed_kmh !== undefined ? Math.round(props.speed_kmh) : 0;
        const vNum = props.vehicle_number || props.id || '---';
        const brigade = props.brigade || '-';
        const time = props.time || 'Brak danych';

        const popupHtml = `
            <div style="padding: 4px;">
                <div style="font-size: 15px; font-weight: 700; color: #8e44ad; margin-bottom: 6px;">
                    🚋 Linia ${props.line} <span style="font-size: 12px; color: #64748b;">(#${vNum})</span>
                </div>
                <div style="font-size: 13px; margin: 3px 0;">
                    ⚡ Prędkość: <strong>${speed} km/h</strong>
                </div>
                <div style="font-size: 12px; color: #475569; margin: 3px 0;">
                    Brygada: <strong>${brigade}</strong>
                </div>
                <div style="font-size: 11px; color: #94a3b8; margin-top: 6px; border-top: 1px solid #e2e8f0; padding-top: 4px;">
                    Odczyt GPS: ${time}
                </div>
            </div>
        `;

        new maplibregl.Popup({ offset: 8 })
            .setLngLat(coords)
            .setHTML(popupHtml)
            .addTo(map);
    });

    map.on('mouseenter', 'tram-live-layer', () => { map.getCanvas().style.cursor = 'pointer'; });
    map.on('mouseleave', 'tram-live-layer', () => { map.getCanvas().style.cursor = ''; });

    // Start odpytywania
    await fetchLiveTrams(map);
    if (tramFetchInterval) clearInterval(tramFetchInterval);
    tramFetchInterval = setInterval(() => fetchLiveTrams(map), 10000);
}