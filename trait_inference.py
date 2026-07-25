# FILE: trait_inference.py
# VERSION: 2.1 - "The Marine Biology Patch"
# RESPONSIBILITY: Instantly maps newly discovered animals to physical traits and the new 5-Tier Size Scale. Includes Marine Life.

import logging

def infer_traits(species_name):
    """
    Analyzes a species name to guess its physical traits for the Forensic Profiler.
    Returns: (Family, Size (1-5), Beak, Color, Silhouette, Sound)
    
    SIZE CLASSES:
    1 = Micro (<1 kg)
    2 = Small (1 - 5 kg)
    3 = Medium (5 - 30 kg)
    4 = Large (30 - 300 kg)
    5 = Megafauna (>300 kg)
    """
    name = species_name.lower().strip()
    
    # --- CLASS 5: MEGAFAUNA (>300kg) ---
    if any(x in name for x in ["whale", "orca", "whale shark", "manta ray"]):
        return "Marine Megafauna", 5, "generalist", "blue", "aquatic", "silent"
    if any(x in name for x in["elephant", "rhino", "hippopotamus", "hippo", "giraffe", "moose", "bear", "panda"]):
        return "Megafauna", 5, "generalist", "gray", "mammal", "rumble"

    # --- CLASS 4: LARGE (30-300kg) ---
    if any(x in name for x in["shark", "dolphin", "porpoise", "beluga", "manatee", "dugong"]):
        return "Large Aquatic", 4, "generalist", "gray", "aquatic", "silent"
    if any(x in name for x in["lion", "tiger", "leopard", "jaguar", "panther", "cheetah", "puma", "cougar"]):
        return "Large Feline", 4, "generalist", "brown", "mammal", "roar"
    if any(x in name for x in["wolf", "hyena", "deer", "elk", "caribou", "antelope", "horse", "zebra", "cow", "cattle", "sheep", "pig", "boar", "bovid", "wildebeest"]):
        return "Large Mammal", 4, "generalist", "brown", "mammal", "call"
    if any(x in name for x in["sea lion", "seal", "walrus"]):
        return "Marine Mammal", 4, "generalist", "gray", "mammal", "bark"
    if "human" in name or "person" in name:
        return "Human", 4, "generalist", "multi", "mammal", "voice"

    # --- CLASS 3: MEDIUM (5-30kg) ---
    if any(x in name for x in ["octopus", "squid", "cuttlefish"]):
        return "Cephalopod", 3, "generalist", "multi", "aquatic", "silent"
    if any(x in name for x in["salmon", "tuna", "grouper", "eel", "barracuda", "stingray", "sea turtle"]):
        return "Medium Aquatic", 3, "generalist", "multi", "aquatic", "silent"
    if any(x in name for x in["fox", "coyote", "raccoon", "monkey", "ape", "gorilla", "chimpanzee", "baboon", "macaque", "lemur", "badger", "otter", "dog", "cat", "lynx", "marmot", "wolverine"]):
        return "Medium Mammal", 3, "generalist", "brown", "mammal", "call"
    if any(x in name for x in["eagle", "vulture", "condor", "stork", "pelican", "swan", "crane", "turkey", "bustard"]):
        return "Large Bird", 3, "hook", "brown", "soaring", "call"

    # --- CLASS 2: SMALL (1-5kg) ---
    if any(x in name for x in["fish", "trout", "bass", "carp", "koi", "catfish", "snapper", "flounder", "mackerel"]):
        return "Fish", 2, "generalist", "multi", "aquatic", "silent"
    if any(x in name for x in ["crab", "lobster", "crayfish"]):
        return "Crustacean", 2, "generalist", "red", "aquatic", "silent"
    if any(x in name for x in["jellyfish", "starfish", "urchin", "anemone", "coral", "sponge"]):
        return "Marine Invertebrate", 2, "generalist", "multi", "aquatic", "silent"
    if any(x in name for x in["hawk", "kite", "harrier", "osprey", "buzzard", "owl", "falcon", "kestrel", "merlin"]):
        return "Raptor", 2, "hook", "brown", "soaring", "screech"
    if any(x in name for x in["duck", "mallard", "teal", "wigeon", "goose", "brant", "loon", "grebe", "cormorant", "shag"]):
        return "Waterfowl", 2, "generalist", "brown", "duck", "quack"
    if any(x in name for x in["heron", "egret", "bittern", "ibis", "spoonbill"]):
        return "Wader", 2, "needle", "gray", "wader", "call"
    if any(x in name for x in["gull", "tern", "pheasant", "grouse", "partridge", "guineafowl"]):
        return "Water/Game Bird", 2, "generalist", "white", "soaring", "call"
    if any(x in name for x in["squirrel", "turtle", "rabbit", "hare", "skunk", "mink", "weasel", "mongoose", "iguana", "monitor"]):
        return "Small Animal", 2, "generalist", "brown", "mammal", "call"
    if any(x in name for x in["pigeon", "dove", "crow", "raven", "rook", "jackdaw", "woodpecker", "parrot", "macaw", "cockatoo"]):
        return "Bird", 2, "generalist", "gray", "perching", "call"

    # --- CLASS 1: MICRO (<1kg) ---
    if any(x in name for x in["shrimp", "krill", "plankton", "snail", "clam", "oyster", "mussel", "barnacle"]):
        return "Micro Aquatic", 1, "generalist", "gray", "aquatic", "silent"
    if any(x in name for x in["cricket", "cicada", "grasshopper", "katydid", "insect", "bug"]):
        return "Insect", 1, "generalist", "green", "amphibian", "chirp"
    if any(x in name for x in["frog", "toad", "peeper", "amphibian", "mouse", "rat", "rodent", "bat", "chipmunk", "lizard"]):
        return "Micro Animal", 1, "generalist", "green", "amphibian", "call"
    if any(x in name for x in["sparrow", "finch", "bunting", "wren", "tit", "warbler", "robin", "thrush", "bluebird", "swallow", "swift", "hummingbird", "kinglet", "jay", "magpie", "oriole", "starling", "cardinal", "tanager", "sandpiper", "plover", "snipe"]):
        return "Songbird/Small Bird", 1, "cone", "brown", "perching", "song"

    # --- FALLBACK ---
    return "Passerine", 1, "generalist", "gray", "perching", "call"