let map = null;

function formatTime(val) {
    if (!val) return "Brak danych";
    const date = new Date(val);
    return isNaN(date.getTime()) ? "Aktualne" : date.toLocaleTimeString('pl-PL', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function toggleMobileFilters() {
    const legend = document.getElementById('legend');
    if (legend) {
        legend.classList.toggle('active');
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
        initInfrastructureLayers(map);
        await initBikesLayer(map);
        initTramLiveLayer(map);
    });

    // Zamknięcie wysuwanego menu na mobile po kliknięciu w tło mapy
    map.on('click', (e) => {
        const features = map.queryRenderedFeatures(e.point, { layers: ['stations-point'] });
        if (!features.length) {
            const legend = document.getElementById('legend');
            if (legend && legend.classList.contains('active')) {
                legend.classList.remove('active');
            }
        }
    });

    window.gisMap = map;
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initMap);
} else {
    initMap();
}