# FILE: bioacoustic_profiles.py
# VERSION: 2.1 - "The Electronic Artifact Veto"
# CHANGELOG:
# [2026-09-02 02:13] - v2.1: Implemented global Electronic Artifact veto to reject mic buzzes/sine waves (tonality > 0.85).
# [2026-08-15 10:00] - v2.0: Implemented strict Spectral Flatness checks to kill Rain/Wind false positives. Implemented Frequency Isolation.

import numpy as np
import logging

# --- DSP CORE FUNCTIONS ---

def _read_wav_bytes(wav_bytes):
    try:
        if len(wav_bytes) < 44: return None, 0
        sr = int.from_bytes(wav_bytes[24:28], 'little')
        raw_data = np.frombuffer(wav_bytes[44:], dtype=np.int16)
        float_data = raw_data.astype(np.float32) / 32768.0
        return float_data, sr
    except Exception as e:
        return None, 0

def _calculate_spectral_stats(audio, sr):
    if len(audio) < sr * 0.1: return None 

    n = len(audio)
    w = audio * np.hanning(n)
    fft_res = np.fft.rfft(w)
    mag = np.abs(fft_res)
    power = mag ** 2
    freqs = np.fft.rfftfreq(n, 1/sr)
    
    total_power = np.sum(power) + 1e-12
    
    # Frequency Bands
    # Low: 20-250 Hz (Rumble / Thunder / Wind buffeting)
    e_low = np.sum(power[(freqs >= 20) & (freqs < 250)])
    # Mid: 250-2000 Hz (Animal Calls / Fundamentals)
    e_mid = np.sum(power[(freqs >= 250) & (freqs < 2000)])
    # High: 2000-8000 Hz (Hiss / Rain Splatter / Insects)
    e_high = np.sum(power[(freqs >= 2000) & (freqs < 8000)])
    
    low_ratio = e_low / total_power
    mid_ratio = e_mid / total_power
    high_ratio = e_high / total_power
    
    mid_high_ratio = (e_mid + 1e-12) / (e_high + 1e-12)
    
    dom_idx = np.argmax(mag)
    dom_freq = freqs[dom_idx]
    
    # Spectral Flatness (Closer to 1.0 = White Noise/Rain. Closer to 0.0 = Tonal/Animal)
    gmean = np.exp(np.mean(np.log(power + 1e-12)))
    amean = np.mean(power)
    flatness = gmean / (amean + 1e-12)
    tonality = 1.0 - min(1.0, flatness)
    
    rms = np.sqrt(np.mean(audio**2))
    level_score = float(np.clip((rms - 0.01) / 0.08, 0.0, 1.0))
    
    return {
        "rms": rms,
        "level_score": level_score,
        "low": low_ratio,
        "mid": mid_ratio,
        "high": high_ratio,
        "mid_high_ratio": mid_high_ratio,
        "dom_freq": dom_freq,
        "tonality": tonality,
        "flatness": flatness
    }

# --- ANIMAL DEFINITIONS (WITH STRICT VETOES) ---

def score_cricket(stats):
    # VETO 1: Flatness. Rain is flat. Crickets are piercing.
    if stats['flatness'] > 0.6: return 0.0, "VETO: Broadband Noise (Rain)"
    
    # VETO 2: Isolation. If there is heavy bass (wind/thunder), it's a storm, not a cricket.
    if stats['low'] > 0.4: return 0.0, "VETO: Excessive Low Freq (Wind)"

    score = 0.0
    if stats['dom_freq'] > 2500: score += 0.4
    if stats['high'] > 0.6: score += 0.4
    if stats['tonality'] > 0.3: score += 0.2
    return score, "OK"

def score_elephant(stats):
    # VETO 1: Flatness. 
    if stats['flatness'] > 0.6: return 0.0, "VETO: Broadband Noise (Wind/Rain)"
    
    # VETO 2: Isolation. True rumbles don't have screaming high-frequency hiss.
    if stats['high'] > 0.3: return 0.0, "VETO: Excessive High Freq (Rain/Hiss)"

    score = 0.0
    if stats['low'] > 0.6: score += 0.4
    if stats['dom_freq'] < 150: score += 0.4
    if stats['tonality'] > 0.15: score += 0.2
    return score, "OK"

def score_wolf(stats):
    # VETO 1: Flatness. Howls are extremely pure tones.
    if stats['flatness'] > 0.5: return 0.0, "VETO: Broadband Noise"
    
    # VETO 2: Isolation.
    if stats['low'] > 0.5: return 0.0, "VETO: Excessive Low Freq"

    score = 0.0
    if stats['tonality'] > 0.4: score += 0.4
    if 300 < stats['dom_freq'] < 1200: score += 0.4
    if stats['mid'] > 0.3: score += 0.2
    return score, "OK"

def score_frog(stats):
    if stats['flatness'] > 0.6: return 0.0, "VETO: Broadband Noise"
    if stats['low'] > 0.5: return 0.0, "VETO: Excessive Low Freq"

    score = 0.0
    if stats['high'] > 0.4: score += 0.4
    if stats['tonality'] > 0.2: score += 0.4
    if stats['level_score'] > 0.1: score += 0.2
    return score, "OK"

def score_sea_lion(stats):
    if stats['flatness'] > 0.6: return 0.0, "VETO: Broadband Noise"
    
    mid_boost = float(np.clip(np.tanh((stats['mid_high_ratio'] - 0.7) / 1.0) * 0.5 + 0.5, 0.0, 1.0))
    lf_penalty = float(np.clip((stats['low'] - 0.25) / 0.40, 0.0, 1.0))
    
    score = (0.35 * stats['level_score']) + (0.40 * stats['tonality']) + (0.25 * mid_boost)
    score *= (1.0 - 0.65 * lf_penalty)
    
    if stats['dom_freq'] < 100 or stats['dom_freq'] > 2000:
        score *= 0.5 
    return score, "OK"

def score_generic_mammal_call(stats):
    if stats['flatness'] > 0.6: return 0.0, "VETO: Broadband Noise"
    
    score = 0.0
    if stats['mid'] > 0.3: score += 0.3
    if stats['tonality'] > 0.2: score += 0.3
    if stats['level_score'] > 0.2: score += 0.4
    return score, "OK"

# --- REGISTRY ---

PROFILES = {
    "CRICKET": score_cricket,
    "CICADA": score_cricket,
    
    "ELEPHANT": score_elephant,
    "RHINOCEROS": score_elephant,
    "HIPPOPOTAMUS": score_elephant,
    
    "WOLF": score_wolf,
    "HYENA": score_wolf,
    "COYOTE": score_wolf,
    
    "SEA_LION": score_sea_lion,
    "OTTER": score_sea_lion,
    
    "COW": score_generic_mammal_call,
    "SHEEP": score_generic_mammal_call,
    "YAK": score_generic_mammal_call,
    "ZEBRA": score_generic_mammal_call,
    "CAMEL": score_generic_mammal_call,
    "LION": score_generic_mammal_call,
    "TIGER": score_generic_mammal_call,
    "MONKEY": score_generic_mammal_call,
    "GORILLA": score_generic_mammal_call,
    "PANDA": score_generic_mammal_call,
    
    "FROG": score_frog
}

# --- PUBLIC API ---

def analyze_target(wav_bytes, target_animal):
    target = target_animal.upper().replace(" ", "_")
    if target not in PROFILES: return 0.0, f"Unknown Profile: {target}"
    
    audio, sr = _read_wav_bytes(wav_bytes)
    if audio is None: return 0.0, "Audio Error"
    
    stats = _calculate_spectral_stats(audio, sr)
    if not stats: return 0.0, "Silence/Short"
    
    # --- THE ELECTRONIC ARTIFACT VETO PATCH ---
    # A tonality > 0.85 indicates a near-perfect sine wave (electronic hum/buzz).
    # No biological animal emits a perfectly continuous, pure sine wave for 12 seconds.
    if stats['tonality'] > 0.85:
        return 0.0, f"VETO: Electronic Artifact / Mic Buzz (Tonality {stats['tonality']:.2f})"
    
    # Run the specific scorer
    scorer = PROFILES[target]
    score, veto_reason = scorer(stats)
    
    final_score = min(1.0, max(0.0, score))
    
    if final_score == 0.0 and "VETO" in veto_reason:
        debug = f"{target} | REJECTED | {veto_reason} (Flat: {stats['flatness']:.2f}, Low: {stats['low']:.2f}, High: {stats['high']:.2f})"
    else:
        debug = (f"{target} | Score: {final_score:.2f} | "
                 f"Tone:{stats['tonality']:.2f} Flat:{stats['flatness']:.2f} "
                 f"L:{stats['low']:.2f} M:{stats['mid']:.2f} H:{stats['high']:.2f} "
                 f"Dom:{int(stats['dom_freq'])}Hz")
             
    return final_score, debug