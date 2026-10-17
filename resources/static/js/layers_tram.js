export function setupTramLiveLayers(map) {
    // 1. Źródło pozycji tramwajów na żywo
    map.addSource('tram-live-source', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] }
    });

    // 2. Kółko pozycji wozu (na osi torowiska)
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