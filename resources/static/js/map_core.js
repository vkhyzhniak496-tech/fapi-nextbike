export let map = null;

export function initMapInstance(containerId = 'map', onMapReady) {
    const container = document.getElementById(containerId);
    if (!container) return;

    map = new maplibregl.Map({
        container: containerId,
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

    map.on('load', () => {
        if (onMapReady) onMapReady(map);
    });

    map.on('click', (e) => {
        const interactiveLayers = ['stations-point', 'tram-live-layer', 'tram-dwell-points'];
        const existingLayers = interactiveLayers.filter(id => map.getLayer(id));
        const f = map.queryRenderedFeatures(e.point, { layers: existingLayers });
        
        if (!f.length) {
            const legend = document.getElementById('legend');
            if (legend && legend.classList.contains('active')) {
                legend.classList.remove('active');
            }
        }
    });

    return map;
}