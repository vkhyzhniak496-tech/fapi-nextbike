export const FILTER_RULES = {
  ddr: ['==', ['get', 'infra_type'], 'ddr'],
  lane: ['==', ['get', 'infra_type'], 'lane'],
  woonerf: ['==', ['get', 'infra_type'], 'woonerf'],
  tempo30: ['==', ['get', 'infra_type'], 'tempo30'],
};

export function setupInfraLayers(map) {
  // 1. Ścieżki rowerowe
  map.addSource('safe-cycleways', {
    type: 'geojson',
    data: '/network/safe-cycleways',
  });

  map.addLayer({
    id: 'cycleways-lines',
    type: 'line',
    source: 'safe-cycleways',
    layout: {'line-join': 'round', 'line-cap': 'round'},
    paint: {
      'line-color': [
        'match',
        ['get', 'infra_type'],
        'ddr', '#27ae60',
        'lane', '#f39c12',
        'woonerf', '#2980b9',
        'tempo30', '#3498db',
        '#3498db' // fallback
      ],
      'line-width': 3,
      'line-opacity': 0.85,
    },
  });

  // 2. Torowiska tramwajowe
  map.addSource('tram-network', {
    type: 'geojson',
    data: '/network/tram',
  });

  map.addLayer({
    id: 'tram-tracks',
    type: 'line',
    source: 'tram-network',
    layout: {'line-join': 'round', 'line-cap': 'round'},
    paint: {
      'line-color': '#3b0141',
      'line-width': 2.5,
      'line-opacity': 0.85,
    },
  });

  // 3. Przystanki tramwajowe
  map.addSource('tram-platforms', {
    type: 'geojson',
    data: '/network/tram/platforms',
  });

  map.addLayer({
    id: 'tram-platforms-circle',
    type: 'circle',
    source: 'tram-platforms',
    paint: {
      'circle-radius': 4,
      'circle-color': '#ffffff',
      'circle-stroke-width': 2,
      'circle-stroke-color': '#8e44ad',
    },
  });

  map.addLayer({
    id: 'tram-platforms-label',
    type: 'symbol',
    source: 'tram-platforms',
    minzoom: 13.5,
    layout: {
      'text-field': ['get', 'name'],
      'text-size': 11,
      'text-offset': [0, 1.2],
      'text-anchor': 'top',
      'text-max-width': 10,
    },
    paint: {
      'text-color': '#2c3e50',
      'text-halo-color': '#ffffff',
      'text-halo-width': 1.5,
    },
  });
}