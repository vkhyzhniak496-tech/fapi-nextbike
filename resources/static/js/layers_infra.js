function toggleTramTracks() {
    if (!map) return;
    const checkbox = document.getElementById('filter-trams');
    const isVisible = checkbox ? checkbox.checked : true;
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