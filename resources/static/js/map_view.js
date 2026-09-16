import { map, initMapInstance } from './map_core.js';
import { setupInfraLayers, FILTER_RULES } from './layers_infra.js';
import { setupBikeLayers, fetchStations, handleUrlStationTarget } from './layers_bikes.js';
import { setupTramLiveLayers, fetchLiveTrams } from './layers_tram.js';

function updateCyclewaysFilter() {
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

function toggleTramTracks() {
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

function toggleMobileFilters() {
    const legend = document.getElementById('legend');
    if (legend) legend.classList.toggle('active');
}

function bindUiEvents() {
    document.getElementById('btn-toggle-filters')?.addEventListener('click', toggleMobileFilters);
    document.querySelector('.btn-close-mobile')?.addEventListener('click', toggleMobileFilters);

    ['filter-ddr', 'filter-lane', 'filter-woonerf', 'filter-tempo30'].forEach(id => {
        document.getElementById(id)?.addEventListener('change', updateCyclewaysFilter);
    });

    document.getElementById('filter-trams')?.addEventListener('change', toggleTramTracks);
}

document.addEventListener('DOMContentLoaded', () => {
    bindUiEvents();

    initMapInstance('map', async (mapInstance) => {
        // Rejestracja warstw
        setupInfraLayers(mapInstance);
        setupTramLiveLayers(mapInstance);
        setupBikeLayers(mapInstance);

        // Cykliczne pobieranie danych
        const initialStations = await fetchStations(mapInstance);
        handleUrlStationTarget(mapInstance, initialStations);
        setInterval(() => fetchStations(mapInstance), 30000);

        await fetchLiveTrams(mapInstance);
        setInterval(() => fetchLiveTrams(mapInstance), 10000);
    });
});