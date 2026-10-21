"""German states and metro regions as search areas for source="search".

A region in `locations` ("NRW", "Bayern", "Rhein-Main") is searched through its largest cities, in
population order; the location check still accepts any town of the region. Other places stay as given.
"""
from itertools import zip_longest
import re

# name: (aliases, largest cities). City states stay plain places (Berlin, Hamburg, Bremen).
REGIONS = {
    'Baden-Württemberg': (('bw', 'baden-württemberg', 'baden-wuerttemberg'),
                          ['Stuttgart', 'Mannheim', 'Karlsruhe', 'Freiburg im Breisgau', 'Heidelberg', 'Ulm', 'Heilbronn', 'Pforzheim']),
    'Bayern': (('bayern', 'bavaria', 'freistaat bayern'),
               ['München', 'Nürnberg', 'Augsburg', 'Regensburg', 'Ingolstadt', 'Würzburg', 'Fürth', 'Erlangen']),
    'Brandenburg': (('brandenburg', 'land brandenburg'), ['Potsdam', 'Cottbus', 'Brandenburg an der Havel', 'Frankfurt Oder']),
    'Land Bremen': (('land bremen',), ['Bremen', 'Bremerhaven']),
    'Hessen': (('hessen', 'hesse'),
               ['Frankfurt am Main', 'Wiesbaden', 'Kassel', 'Darmstadt', 'Offenbach am Main', 'Hanau', 'Gießen', 'Marburg']),
    'Mecklenburg-Vorpommern': (('mv', 'mecklenburg-vorpommern', 'meckpomm'),
                               ['Rostock', 'Schwerin', 'Neubrandenburg', 'Stralsund', 'Greifswald']),
    'Niedersachsen': (('niedersachsen', 'lower saxony'),
                      ['Hannover', 'Braunschweig', 'Oldenburg', 'Osnabrück', 'Wolfsburg', 'Göttingen', 'Salzgitter', 'Hildesheim']),
    'Nordrhein-Westfalen': (('nrw', 'nordrhein-westfalen', 'north rhine-westphalia'),
                            ['Köln', 'Düsseldorf', 'Dortmund', 'Essen', 'Duisburg', 'Bochum', 'Wuppertal', 'Bielefeld', 'Bonn', 'Münster']),
    'Rheinland-Pfalz': (('rlp', 'rheinland-pfalz', 'rhineland-palatinate'),
                        ['Mainz', 'Ludwigshafen am Rhein', 'Koblenz', 'Trier', 'Kaiserslautern', 'Worms']),
    'Saarland': (('saarland',), ['Saarbrücken', 'Neunkirchen', 'Homburg', 'Völklingen']),
    'Sachsen': (('sachsen', 'saxony', 'freistaat sachsen'), ['Leipzig', 'Dresden', 'Chemnitz', 'Zwickau', 'Plauen', 'Görlitz']),
    'Sachsen-Anhalt': (('sachsen-anhalt', 'saxony-anhalt'), ['Halle', 'Magdeburg', 'Dessau-Roßlau', 'Lutherstadt Wittenberg']),
    'Schleswig-Holstein': (('sh', 'schleswig-holstein'), ['Kiel', 'Lübeck', 'Flensburg', 'Neumünster', 'Norderstedt']),
    'Thüringen': (('thüringen', 'thueringen', 'thuringia'), ['Erfurt', 'Jena', 'Gera', 'Weimar', 'Gotha']),
    'Ruhrgebiet': (('ruhrgebiet', 'ruhrpott', 'ruhr'),
                   ['Dortmund', 'Essen', 'Duisburg', 'Bochum', 'Gelsenkirchen', 'Oberhausen', 'Hagen', 'Mülheim an der Ruhr']),
    'Rhein-Main': (('rhein-main', 'rhein-main-gebiet', 'rhein main'),
                   ['Frankfurt am Main', 'Wiesbaden', 'Mainz', 'Darmstadt', 'Offenbach am Main', 'Hanau']),
    'Rhein-Neckar': (('rhein-neckar', 'metropolregion rhein-neckar'), ['Mannheim', 'Heidelberg', 'Ludwigshafen am Rhein']),
    'Deutschland': (('deutschland', 'germany', 'bundesweit'),
                    ['Berlin', 'Hamburg', 'München', 'Köln', 'Frankfurt am Main', 'Stuttgart', 'Düsseldorf', 'Leipzig',
                     'Dortmund', 'Essen', 'Bremen', 'Dresden', 'Hannover', 'Nürnberg']),
}
ALIASES = {alias: name for name, (aliases, _) in REGIONS.items() for alias in aliases + (name.casefold(),)}


def region_of(place):
    """Region name for a place string ("NRW", "nordrhein westfalen"), else None."""
    key = re.sub(r'\s+', ' ', (place or '').replace('–', '-').strip().casefold())
    return ALIASES.get(key) or ALIASES.get(key.replace(' ', '-'))


def expand_locations(locations):
    """(places to search, regions found). Regions become their cities; places of several entries take
    turns (Köln, München, Düsseldorf, …) and appear once (München and Bayern)."""
    groups, regions = [], []
    for loc in locations:
        region = region_of(loc)
        if region: regions.append(region)
        groups.append(REGIONS[region][1] if region else [loc])
    places = [p for row in zip_longest(*groups) for p in row if p]
    return list(dict.fromkeys(places)), list(dict.fromkeys(regions))
