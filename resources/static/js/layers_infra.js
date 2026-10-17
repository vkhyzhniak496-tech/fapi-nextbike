// 1. Wydzielona droga dla rowerów (zielona)
const IS_DDR = [
  'any',
  ['==', ['get', 'highway'], 'cycleway'],
  ['all', ['==', ['get', 'highway'], 'path'], ['==', ['get', 'bicycle'], 'designated']],
  ['all', ['==', ['get', 'highway'], 'footway'], ['==', ['get', 'bicycle'], 'designated']]
];

// 2. Pas rowerowy w jezdni / kontrapas / buspas z dopuszczeniem rowerów (pomarańczowy)
const HAS_LANE = [
  'any',
  ['in', ['get', 'cycleway'], ['literal', ['lane', 'opposite_lane', 'share_busway', 'track', 'opposite_track', 'yes']]],
  ['in', ['get', 'cycleway:left'], ['literal', ['lane', 'opposite_lane', 'share_busway', 'track', 'yes']]],
  ['in', ['get', 'cycleway:right'], ['literal', ['lane', 'opposite_lane', 'share_busway', 'track', 'yes']]],
  ['in', ['get', 'cycleway:both'], ['literal', ['lane', 'opposite_lane', 'share_busway', 'track', 'yes']]],
  ['==', ['get', 'oneway:bicycle'], 'no'],
  ['==', ['get', 'cycleway:opposite'], 'yes']
];

// 3. Strefa zamieszkania / Woonerf (ciemnoniebieski)
const IS_WOONERF = ['==', ['get', 'highway'], 'living_street'];

// 4. Ulica uspokojona / Tempo 30 (jasnoniebieski)
const IS_TEMPO30 = [
  'all',
  ['!', IS_DDR],
  ['!', HAS_LANE],
  ['!', IS_WOONERF],
  ['any',
    ['==', ['get', 'maxspeed'], '30'],
    ['==', ['get', 'zone:maxspeed'], 'PL:zone30'],
    ['==', ['get', 'highway'], 'residential']
  ]
];

export const FILTER_RULES = {
  ddr: IS_DDR,
  lane: ['all', ['!', IS_DDR], HAS_LANE],
  woonerf: ['all', ['!', IS_DDR], ['!', HAS_LANE], IS_WOONERF],
  tempo30: IS_TEMPO30,
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
        'case',
        FILTER_RULES.ddr,
        '#27ae60',
        FILTER_RULES.lane,
        '#f39c12',
        FILTER_RULES.woonerf,
        '#2980b9',
        '#3498db',
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