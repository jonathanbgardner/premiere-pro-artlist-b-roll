"""Offline (no-API) candidate generator. Simple, deterministic keyword rules:
good enough to exercise the whole pipeline and as a talking-head filler, not a
substitute for the model's judgment."""
from __future__ import annotations

import re
from collections import Counter

from .beats import Beat
from .candidates import Candidate, MusicSpec
from .transcript import Segment

# word -> (visual phrase, category). Plural/inflected forms are matched via _stem.
LEXICON: dict[str, tuple[str, str]] = {
    # farm / rural
    "cattle": ("cattle herd", "farm"), "cow": ("cows grazing", "farm"), "herd": ("cattle herd", "farm"),
    "calf": ("newborn calf", "farm"), "barn": ("red barn", "farm"), "tractor": ("tractor plowing field", "farm"),
    "field": ("farm field", "farm"), "crop": ("crop rows", "farm"), "wheat": ("wheat field", "farm"),
    "corn": ("corn field", "farm"), "grain": ("grain harvest", "farm"), "harvest": ("harvest combine", "farm"),
    "combine": ("combine harvester", "farm"), "soil": ("soil close up", "farm"), "seed": ("planting seeds", "farm"),
    "farm": ("family farm", "farm"), "farmer": ("farmer working", "farm"), "ranch": ("cattle ranch", "farm"),
    "pasture": ("green pasture", "farm"), "fence": ("farm fence", "farm"), "hay": ("hay bales", "farm"),
    "chicken": ("chickens farmyard", "farm"), "egg": ("fresh eggs", "farm"), "milk": ("milking cows", "farm"),
    "dairy": ("dairy farm", "farm"), "orchard": ("apple orchard", "farm"), "irrigation": ("irrigation sprinkler", "farm"),
    "rain": ("rain falling", "nature"), "drought": ("dry cracked earth", "nature"), "river": ("river flowing", "nature"),
    "creek": ("small creek", "nature"), "mountain": ("mountain landscape", "nature"), "forest": ("forest trees", "nature"),
    "tree": ("trees in wind", "nature"), "sunrise": ("sunrise", "nature"), "dawn": ("sunrise", "nature"),
    "sunset": ("sunset", "nature"), "storm": ("storm clouds", "nature"), "snow": ("snowfall", "nature"),
    "winter": ("winter landscape", "nature"), "ocean": ("ocean waves", "nature"), "beach": ("beach", "nature"),
    "lake": ("lake", "nature"), "sky": ("sky timelapse", "nature"), "weather": ("weather clouds", "nature"),
    "bird": ("birds flying", "nature"), "bee": ("bees on flowers", "nature"), "flower": ("flowers", "nature"),
    # town / city
    "town": ("small town", "town"), "street": ("main street", "town"), "city": ("city skyline", "city"),
    "downtown": ("downtown street", "city"), "traffic": ("traffic", "city"), "highway": ("highway traffic", "city"),
    "road": ("country road", "town"), "truck": ("pickup truck driving", "town"), "car": ("car driving", "city"),
    "store": ("store front", "town"), "shop": ("small shop", "town"), "market": ("farmers market", "town"),
    "restaurant": ("restaurant kitchen", "food"), "school": ("school building", "town"), "church": ("church", "town"),
    "bank": ("bank building", "business"), "office": ("office", "business"), "factory": ("factory", "business"),
    "warehouse": ("warehouse", "business"), "train": ("train", "city"), "airport": ("airport", "city"),
    # food
    "coffee": ("coffee pouring", "food"), "breakfast": ("breakfast table", "food"), "bread": ("fresh bread", "food"),
    "kitchen": ("kitchen cooking", "food"), "dinner": ("family dinner", "food"), "beef": ("beef steak", "food"),
    "food": ("food close up", "food"), "grocery": ("grocery store aisle", "food"), "vegetable": ("fresh vegetables", "food"),
    # business / money
    "price": ("price tags", "business"), "money": ("money cash", "business"), "loan": ("signing documents", "business"),
    "contract": ("signing contract", "business"), "cost": ("calculator bills", "business"),
    "dollar": ("dollar bills", "business"), "budget": ("budget spreadsheet", "business"),
    "customer": ("customer shopping", "business"), "meeting": ("business meeting", "business"),
    # tech
    "phone": ("smartphone in hand", "tech"), "app": ("phone app", "tech"), "computer": ("laptop typing", "tech"),
    "laptop": ("laptop typing", "tech"), "drone": ("drone flying", "tech"), "satellite": ("satellite orbit", "tech"),
    "sensor": ("sensor device", "tech"), "data": ("data dashboard", "tech"), "internet": ("internet connection", "tech"),
    "screen": ("screen close up", "tech"), "camera": ("camera", "tech"),
    # people
    "family": ("family together", "people"), "kid": ("kids playing", "people"), "child": ("children playing", "people"),
    "daughter": ("father and daughter", "people"), "son": ("father and son", "people"), "neighbor": ("neighbors talking", "people"),
    "community": ("community gathering", "people"), "crowd": ("crowd", "people"), "hand": ("hands close up", "people"),
    "worker": ("workers", "people"), "grandfather": ("old man portrait", "people"), "grandmother": ("old woman portrait", "people"),
}

ACTIONS: dict[str, str] = {
    "feeding": "feeding", "feed": "feeding", "fed": "feeding", "driving": "driving", "drive": "driving",
    "drove": "driving", "planting": "planting", "plant": "planting", "harvesting": "harvesting",
    "cooking": "cooking", "walking": "walking", "walk": "walking", "running": "running", "building": "building",
    "fixing": "repairing", "repairing": "repairing", "fishing": "fishing", "milking": "milking",
    "loading": "loading", "selling": "selling", "buying": "buying", "typing": "typing", "welding": "welding",
    "digging": "digging", "watering": "watering", "riding": "riding", "hiking": "hiking", "swimming": "swimming",
}

# Abstract or microscopic things stock rarely covers well -> ai_video.
ABSTRACT: dict[str, tuple[str, list[str]]] = {
    "microbiome": ("Macro cinematic shot of soil microbes and fungal threads glowing under a microscope, slow push-in",
                   ["soil microbes microscope", "soil close up macro", "healthy soil"]),
    "microbe": ("Macro cinematic shot of microbes moving under a microscope, soft bioluminescent light",
                ["microbes microscope", "microscope science"]),
    "bacteria": ("Microscopic view of bacteria colonies, cinematic lighting, slow drift",
                 ["bacteria microscope", "microscope science"]),
    "fungal": ("Time-lapse of fungal mycelium network spreading through dark soil, glowing threads",
               ["mycelium network timelapse", "mushroom roots soil"]),
    "mycelium": ("Time-lapse of fungal mycelium network spreading through dark soil, glowing threads",
                 ["mycelium network timelapse", "mushroom roots soil"]),
    "molecule": ("Abstract 3D molecules rotating in soft light", ["molecules 3d animation", "science abstract"]),
    "dna": ("Slowly rotating DNA double helix, cinematic depth of field", ["dna helix animation", "science abstract"]),
    "algorithm": ("Abstract flowing lines of data forming a decision tree, dark background",
                  ["data network abstract", "technology abstract"]),
    "photosynthesis": ("Close-up of a leaf with glowing energy flowing through its veins, sunlight",
                       ["leaf sunlight macro", "green leaves sun"]),
}

STAT_RE = re.compile(
    r"(\$\s?\d[\d,.]*\s*(?:million|billion|thousand|k)?|\b\d[\d,.]*\s?(?:%|percent\b)"
    r"|\b\d[\d,.]*\s+(?:million|billion|thousand|hundred|acres?|head|tons?|bushels?|dollars|"
    r"years?|miles|gallons|pounds|cows|hours|people|families)\b)", re.I)
STOP_CAPS = {"I", "I'm", "I've", "I'd", "I'll", "OK", "Okay", "So", "And", "But", "The", "A", "Yeah", "Yes",
             "No", "Well", "Oh", "We", "You", "It", "That", "This", "There", "They", "He", "She", "My", "Our",
             "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "Mr", "Mrs", "Dr"}
MODIFIERS = {"dawn": "sunrise", "sunrise": "sunrise", "morning": "morning", "night": "night", "evening": "evening",
             "winter": "winter", "summer": "summer", "spring": "spring", "fall": "autumn", "autumn": "autumn",
             "old": "vintage", "rain": "rain", "snow": "snow", "aerial": "aerial"}
BROAD = {"farm": "rural farmland", "nature": "nature landscape", "town": "small town life", "city": "city life",
         "food": "food preparation", "business": "small business", "tech": "technology close up",
         "people": "people lifestyle"}
GENRE = {"farm": "acoustic folk", "nature": "ambient cinematic", "town": "acoustic americana", "city": "lo-fi beat",
         "food": "warm acoustic", "business": "corporate light", "tech": "electronic ambient", "people": "warm acoustic"}


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z'\-]*", text)


def _stem(w: str) -> str:
    w = w.lower().strip("'-")
    for suf, rep in (("ies", "y"), ("ves", "f"), ("es", ""), ("s", "")):
        if w.endswith(suf) and w[: -len(suf)] + rep in LEXICON:
            return w[: -len(suf)] + rep
    if w == "cows" or w == "cattle":
        return "cow" if w == "cows" else "cattle"
    if w == "children":
        return "child"
    return w


PLACE_RE = re.compile(
    r"\b(?:in|at|from|outside|near|into|on|across|around|through|visit|visited)\s+(?:the\s+)?"
    r"((?:[A-Z][a-z]+)(?:\s+[A-Z][a-z]+){0,2}(?:,\s*[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)?)")


def _places(text: str, speakers: set[str]) -> list[str]:
    """Capitalized names after a place preposition ("outside Billings, Montana")."""
    out = []
    for m in PLACE_RE.finditer(text):
        name = m.group(1).replace(",", "")
        first = name.split()[0]
        if first in STOP_CAPS or name in speakers or any(name in sp or sp in name for sp in speakers):
            continue
        if name not in out:
            out.append(name)
    return out


def analyze(text: str, speakers: set[str] | None = None) -> dict:
    words = _words(text)
    # capitalized mid-sentence words are names ("Field Notes"), not objects
    stems = [_stem(w) for i, w in enumerate(words) if i == 0 or not w[0].isupper()]
    nouns = [s for s in dict.fromkeys(stems) if s in LEXICON]
    # time-of-day/season words and people are weaker visuals than objects/places
    nouns.sort(key=lambda n: (n in MODIFIERS, LEXICON[n][1] == "people"))
    actions = list(dict.fromkeys(ACTIONS[w.lower()] for w in words if w.lower() in ACTIONS))
    abstract = [s for s in dict.fromkeys(stems) if s in ABSTRACT]
    mods = list(dict.fromkeys(MODIFIERS[s] for s in stems if s in MODIFIERS))
    stats = [m.group(0) for m in STAT_RE.finditer(text)]
    places = _places(text, speakers or set())
    score = 2 * len(abstract) + 2 * len(stats) + len(nouns) + len(actions) + 2 * len(places)
    return {"nouns": nouns, "actions": actions, "abstract": abstract, "mods": mods,
            "stats": stats, "places": places, "score": score}


def _category(nouns: list[str], fallback: str = "people") -> str:
    cats = Counter(LEXICON[n][1] for n in nouns)
    return min(cats, key=lambda k: (-cats[k], k)) if cats else fallback


def queries_for(a: dict, topic: str) -> list[str]:
    """2-3 Artlist queries, specific -> broad."""
    nouns, actions, mods, places = a["nouns"], a["actions"], a["mods"], a["places"]
    cat = LEXICON[nouns[0]][1] if nouns else topic
    qs: list[str] = []
    if a["abstract"]:
        qs += ABSTRACT[a["abstract"][0]][1]
    if places:
        qs.append(f"{places[0]} aerial")
    if nouns:
        main = LEXICON[nouns[0]][0]
        extra = next((x for x in actions + [m for m in mods] if x not in main), None)
        if extra:
            qs.append(f"{main} {extra}")
        elif not places and len(main.split()) <= 2:
            qs.append(f"{main} close up")
        qs.append(main)
    elif actions:
        qs += [f"person {actions[0]} close up", actions[0]]
    if a["stats"] and not nouns and not places:
        qs.insert(0, "rising graph chart animation")
    qs.append(BROAD.get(cat, "lifestyle b-roll"))
    out: list[str] = []
    for q in qs:
        if q.lower() not in (x.lower() for x in out):
            out.append(q)
    return out[:3]


def _trigger(a: dict) -> str:
    for key, name in (("abstract", "abstract"), ("stats", "stat"), ("places", "place"),
                      ("nouns", "concrete_noun"), ("actions", "action")):
        if a[key]:
            return name
    return "talking_head"


def _reason(a: dict, seg_text: str) -> str:
    snippet = seg_text if len(seg_text) <= 70 else seg_text[:67].rsplit(" ", 1)[0] + "..."
    t = _trigger(a)
    label = {"abstract": "Abstract idea", "stat": f"Stat ({a['stats'][0] if a['stats'] else ''})",
             "place": f"Place ({a['places'][0] if a['places'] else ''})", "concrete_noun": "Mentions "
             + ", ".join(a["nouns"][:2]), "action": "Action: " + (a["actions"][0] if a["actions"] else ""),
             "talking_head": "Talking-head cutaway"}[t]
    return f'{label}: "{snippet}"'


def transcript_topic(segments: list[Segment]) -> str:
    a = analyze(" ".join(s.text for s in segments))
    return _category(a["nouns"], "people")


def candidate_from_text(start_s: float, duration_s: float, text: str, topic: str,
                        speakers: set[str] | None = None, priority: int | None = None) -> Candidate | None:
    a = analyze(text, speakers)
    if a["score"] == 0:
        return None
    qs = queries_for(a, topic)
    if a["abstract"]:
        prompt = ABSTRACT[a["abstract"][0]][0] + ", 4K, no text"
        return Candidate(start_s, duration_s, "ai_video", _reason(a, text), qs,
                         ai_prompt=prompt, priority=1, trigger="abstract")
    return Candidate(start_s, duration_s, "stock_video", _reason(a, text), qs,
                     priority=priority or (1 if a["score"] >= 3 else 2), trigger=_trigger(a))


def offline_candidates(segments: list[Segment], beats: list[Beat]) -> list[Candidate]:
    """At most one candidate per beat (two for beats over 20 s), on the segment
    with the strongest visual trigger."""
    topic = transcript_topic(segments)
    speakers = {s.speaker for s in segments if s.speaker}
    out: list[Candidate] = []
    for beat in beats:
        scored = []
        for i in beat.segment_indices:
            seg = segments[i]
            a = analyze(seg.text, speakers)
            if a["score"]:
                scored.append((-a["score"], seg.start_s, i))
        scored.sort()
        picks = []
        for _, start, i in scored:
            if all(abs(start - segments[j].start_s) >= 8 for j in picks):
                picks.append(i)
            if len(picks) >= (2 if beat.duration_s > 20 else 1):
                break
        for i in sorted(picks):
            seg = segments[i]
            dur = min(6.0, max(4.0, seg.duration_s + 1.0))
            c = candidate_from_text(seg.start_s, dur, seg.text, topic, speakers)
            if c:
                out.append(c)
    return out


def make_filler(segments: list[Segment]):
    """Filler for uncovered talking-head stretches: queries from what is said
    around that point, else from the overall topic."""
    topic = transcript_topic(segments)
    speakers = {s.speaker for s in segments if s.speaker}

    def filler(start_s: float, duration_s: float, avoid: frozenset[str] = frozenset()) -> Candidate:
        near = [s for s in segments if s.end_s > start_s - 2 and s.start_s < start_s + duration_s + 4]
        text = " ".join(s.text for s in near)
        c = candidate_from_text(start_s, duration_s, text, topic, speakers, priority=3)
        if c is not None and c.queries and c.queries[0].lower() in avoid:
            c = None
        if c is None or c.type == "ai_video":
            qs = [f"{BROAD.get(topic, 'lifestyle')} detail", BROAD.get(topic, "lifestyle b-roll"), "lifestyle b-roll"]
            snippet = text[:60].rsplit(" ", 1)[0] + "..." if len(text) > 60 else text
            c = Candidate(start_s, duration_s, "stock_video", f'Talking-head cutaway: "{snippet}"', qs, priority=3)
        c.trigger = "talking_head"
        c.reason = "Long talking-head stretch; " + c.reason
        return c

    return filler


def default_music(segments: list[Segment]):
    topic = transcript_topic(segments)
    genre = GENRE.get(topic, "warm acoustic")
    table = {
        "intro": ("warm, inviting", "medium, 90-100 bpm", [f"{genre} intro warm", f"{genre} opening", "warm hopeful intro"]),
        "main": ("light, unobtrusive underscore", "steady, 90-110 bpm", [f"{genre} underscore light", f"{genre} background", "soft documentary underscore"]),
        "outro": ("uplifting, resolving", "medium, 90-100 bpm", [f"{genre} uplifting ending", f"{genre} outro", "hopeful ending"]),
    }

    def spec(section: str) -> MusicSpec:
        mood, tempo, qs = table[section]
        return MusicSpec(section, mood, tempo, qs)

    return spec
