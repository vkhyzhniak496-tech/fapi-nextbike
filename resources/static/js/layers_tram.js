export function setupTramLiveLayers(map) {
    // 1. Źródło pozycji tramwajów na żywo
    map.addSource('tram-live-source', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] }
    });

    // 2. Kółko pozycji wozu (dokładnie na szynie torowiska)
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

    // 3. Etykieta numeru linii tramwajowej
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

    // 4. Prosty popup z danymi identyfikacyjnymi wozu (bez prędkości)
    map.on('click', 'tram-live-layer', (e) => {
        const p = e.features[0].properties;
        const coords = e.features[0].geometry.coordinates.slice();
        const vNum = p.vehicle_number || p.id || '---';

        new maplibregl.Popup({ offset: 8 })
            .setLngLat(coords)
            .setHTML(`
                <div style="font-family: sans-serif; min-width: 130px;">
                    <div style="font-size: 15px; font-weight: 800; color: #8e44ad; margin-bottom: 6px;">
                        🚋 Linia ${p.line}
                    </div>
                    <div style="font-size: 13px; color: #334155; margin-bottom: 2px;">
                        Wóz: <b>#${vNum}</b>
                    </div>
                    <div style="font-size: 12px; color: #64748b; margin-bottom: 4px;">
                        Brygada: <b>${p.brigade || '-'}</b>
                    </div>
                    <div style="font-size: 10px; color: #94a3b8; border-top: 1px solid #f1f5f9; padding-top: 4px;">
                        GPS: ${p.time || ''}
                    </div>
                </div>
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