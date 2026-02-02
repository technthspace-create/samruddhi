"""
Samruddhi Enterprises - SS Steel Pipe Cutting Instruction System
All lengths in millimeters. "Cut 7 × 5000 mm → 65000 mm remaining." One raw pipe = one length (mm).
Uses db.py for storage: SQLite locally, Turso on Vercel when env vars are set.

Extended: Multi-size cutting plan (FFD bin packing) with standard raw length 3600 mm.
"""

import os
from flask import Flask, render_template, request

import db

app = Flask(__name__)

# Standard raw pipe length for multi-size mode (mm). Fixed, no user input.
STANDARD_RAW_LENGTH_MM = 3600.0

# Kerf loss: fixed waste per physical cut (mm). Applied PER CUT in all calculations.
KERF_MM = 3.0

# Scrap below this length is not saved to inventory. Only pieces >= this are stored.
SCRAP_SAVE_THRESHOLD_MM = 100.0

# Scrap classification: ideal reusable range 700–800 mm; avoid tiny scrap < 200 mm.
SCRAP_IDEAL_MIN_MM = 700.0
SCRAP_IDEAL_MAX_MM = 800.0
SCRAP_POOR_THRESHOLD_MM = 200.0


def classify_scrap_mm(scrap_mm):
    """
    Classify scrap length for reporting: IDEAL (700–800), POOR (< 200), else ACCEPTABLE.
    All units mm.
    """
    if scrap_mm < SCRAP_POOR_THRESHOLD_MM:
        return "POOR"
    if SCRAP_IDEAL_MIN_MM <= scrap_mm <= SCRAP_IDEAL_MAX_MM:
        return "IDEAL"
    return "ACCEPTABLE"

# Not used here; db module handles storage
get_leftovers_sorted = db.get_leftovers_sorted
delete_leftover = db.delete_leftover
insert_leftover = db.insert_leftover
init_db = db.init_db


def run_multi_size_plan(cut_requirements):
    """
    Multi-size cutting plan using First-Fit Decreasing (FFD) bin packing.
    cut_requirements: list of (length_mm, quantity) e.g. [(243, 3), (2342, 5), (1500, 2)]
    Raw pipe length is fixed at STANDARD_RAW_LENGTH_MM (3600 mm).
    Kerf loss: 3 mm PER CUT is included in consumed length; scrap is computed after kerf.
    Returns list of { pipe_number, cuts, num_cuts, kerf_mm, used, scrap, scrap_class }.
    """
    if not cut_requirements:
        return {"pipes": [], "total_pipes": 0, "total_used": 0, "total_scrap": 0, "total_kerf": 0}

    # 1. Expand to flat list and 2. sort descending (FFD)
    flat = []
    for length_mm, qty in cut_requirements:
        L = round(float(length_mm), 2)
        n = int(qty)
        if L <= 0 or n <= 0:
            continue
        flat.extend([L] * n)
    flat.sort(reverse=True)

    if not flat:
        return {"pipes": [], "total_pipes": 0, "total_used": 0, "total_scrap": 0, "total_kerf": 0}

    # 3. First-Fit with kerf: each cut consumes piece_length + KERF_MM.
    #    Simulate: new_used = current_used + piece_length + KERF_MM; place only if new_used <= 3600.
    pipes = []  # list of {"remaining": float, "cuts": list}; remaining = 3600 - used (used includes kerf)

    for cut in flat:
        # Need at least (cut + KERF_MM) remaining to place this cut
        needed = cut + KERF_MM
        placed = False
        for p in pipes:
            if p["remaining"] >= needed:
                p["cuts"].append(cut)
                p["remaining"] = round(p["remaining"] - needed, 2)
                placed = True
                break
        if not placed:
            # New pipe: consume (cut + KERF_MM)
            pipes.append({"remaining": round(STANDARD_RAW_LENGTH_MM - needed, 2), "cuts": [cut]})

    # 4. Build output: pipe_number, cuts, num_cuts, kerf_mm, used (incl. kerf), scrap, scrap_class
    out = []
    total_used = 0
    total_scrap = 0
    total_kerf = 0
    for i, p in enumerate(pipes):
        num_cuts = len(p["cuts"])
        pieces_only = round(sum(p["cuts"]), 2)
        kerf_mm = round(num_cuts * KERF_MM, 2)
        used = round(pieces_only + kerf_mm, 2)
        scrap = round(p["remaining"], 2)
        total_used += used
        total_scrap += scrap
        total_kerf += kerf_mm
        out.append({
            "pipe_number": i + 1,
            "cuts": p["cuts"],
            "num_cuts": num_cuts,
            "kerf_mm": kerf_mm,
            "used": used,
            "scrap": scrap,
            "scrap_class": classify_scrap_mm(scrap),
        })

    return {
        "pipes": out,
        "total_pipes": len(out),
        "total_used": round(total_used, 2),
        "total_scrap": round(total_scrap, 2),
        "total_kerf": round(total_kerf, 2),
        "raw_length": STANDARD_RAW_LENGTH_MM,
    }


def run_cutting_plan(raw_material_length, cut_length, quantity_required):
    """
    Build a cutting plan as segments: per pipe/leftover, how many pieces to cut and what remains.
    All lengths in millimeters. Uses leftovers first (largest first), then one new raw pipe at a time.
    """
    cut_length = round(float(cut_length), 2)
    raw_material_length = round(float(raw_material_length), 2)
    quantity_required = int(quantity_required)

    if cut_length <= 0 or quantity_required <= 0:
        return {
            "pieces_produced": 0,
            "material_used": 0.0,
            "scrap_saved_list": [],
            "used_leftover": False,
            "segments": [],
            "suggested_raw": None,
        }

    leftovers = get_leftovers_sorted()
    leftover_index = 0
    scrap_to_save = []
    segments = []
    used_leftover_ids = []
    used_leftover = False
    pieces_from_current = 0
    source_initial_length = 0.0
    current_source_label = ""
    current_leftover_id = None

    # Per cut we consume cut_length + KERF_MM; source must have at least that much to make one cut.
    needed_per_cut = cut_length + KERF_MM

    def next_source():
        """Next usable leftover (largest first) or one new raw pipe. Must fit at least one cut (piece + kerf)."""
        nonlocal leftover_index
        while leftover_index < len(leftovers):
            row = leftovers[leftover_index]
            leftover_index += 1
            if row["length"] >= needed_per_cut:
                return row["length"], "Leftover", row["id"], "Leftover ({:.2f} mm)".format(row["length"])
        return raw_material_length, "Raw material", None, "Raw pipe ({:.2f} mm)".format(raw_material_length)

    available_length, current_source, current_leftover_id, current_source_label = next_source()
    source_initial_length = available_length

    while quantity_required > 0 and cut_length > 0:
        if available_length >= needed_per_cut:
            # Cut one piece: consume piece length + kerf (3 mm per cut)
            pieces_from_current += 1
            quantity_required -= 1
            available_length = round(available_length - needed_per_cut, 2)
        else:
            # Finish this source: record segment, save scrap, move to next
            remaining = round(available_length, 2)
            if pieces_from_current > 0 or remaining > 0:
                segments.append({
                    "source": current_source_label,
                    "source_length": source_initial_length,
                    "pieces": pieces_from_current,
                    "cut_length": cut_length,
                    "remaining": remaining,
                })
            if remaining >= SCRAP_SAVE_THRESHOLD_MM:
                scrap_to_save.append(remaining)
            if current_leftover_id is not None:
                used_leftover_ids.append(current_leftover_id)
                used_leftover = True
            pieces_from_current = 0
            available_length, current_source, current_leftover_id, current_source_label = next_source()
            source_initial_length = available_length
            if quantity_required > 0 and available_length < needed_per_cut:
                break

    # Record final segment when we fulfilled quantity (exited by cutting, not by “no more source”)
    if quantity_required == 0:
        remaining = round(available_length, 2)
        segments.append({
            "source": current_source_label,
            "source_length": source_initial_length,
            "pieces": pieces_from_current,
            "cut_length": cut_length,
            "remaining": remaining,
        })
        if remaining >= SCRAP_SAVE_THRESHOLD_MM:
            scrap_to_save.append(remaining)
        if current_leftover_id is not None:
            used_leftover_ids.append(current_leftover_id)
            used_leftover = True

    # "Add remaining" suggestion: last segment was raw and has remaining
    suggested_raw = None
    if segments and segments[-1]["source"].startswith("Raw pipe") and segments[-1]["remaining"] > 0:
        suggested_raw = round(segments[-1]["remaining"], 2)

    for lid in used_leftover_ids:
        delete_leftover(lid)
    for scrap in scrap_to_save:
        insert_leftover(scrap)

    total_pieces = sum(s["pieces"] for s in segments)
    # Material used: piece lengths only (for reporting); consumed length includes kerf (pieces * KERF_MM).
    material_used = round(total_pieces * cut_length, 2)
    material_used_incl_kerf = round(total_pieces * needed_per_cut, 2)

    return {
        "pieces_produced": total_pieces,
        "material_used": material_used,
        "material_used_incl_kerf": material_used_incl_kerf,
        "kerf_total_mm": round(total_pieces * KERF_MM, 2),
        "scrap_saved_list": scrap_to_save,
        "used_leftover": used_leftover,
        "segments": segments,
        "suggested_raw": suggested_raw,
    }


@app.route("/", methods=["GET", "POST"])
def index():
    init_db()  # ensure table exists (needed on Vercel; no-op if already exists)
    result = None
    multi_result = None

    if request.method == "POST":
        if request.form.get("multi_submit"):
            # Multi-size cutting plan (FFD, 3600 mm standard raw)
            cut_lengths = request.form.getlist("multi_cut_length")
            quantities = request.form.getlist("multi_quantity")
            pairs = []
            for c, q in zip(cut_lengths, quantities):
                try:
                    cv, qv = round(float(c), 2), int(q)
                    if cv > 0 and qv > 0:
                        pairs.append((cv, qv))
                except (TypeError, ValueError):
                    continue
            if pairs:
                multi_result = run_multi_size_plan(pairs)
                # Multi-size scraps: save to database only if >= 100 mm; do not save if under 100 mm
                if multi_result and multi_result.get("pipes"):
                    for p in multi_result["pipes"]:
                        scrap = p.get("scrap", 0)
                        if scrap >= SCRAP_SAVE_THRESHOLD_MM:
                            insert_leftover(scrap)
        else:
            # Existing single-size plan
            try:
                raw_length = float(request.form.get("raw_length", 0))
                cut_length = float(request.form.get("cut_length", 0))
                quantity_required = int(request.form.get("quantity_required", 0))
            except (TypeError, ValueError):
                raw_length = cut_length = quantity_required = 0

            if raw_length > 0 and cut_length > 0 and quantity_required > 0:
                result = run_cutting_plan(raw_length, cut_length, quantity_required)

    # Prefill form from query (e.g. "Use 65000 mm" link: /?raw_length=65000)
    prefill = {
        "raw_length": request.args.get("raw_length", ""),
        "cut_length": request.args.get("cut_length", ""),
        "quantity_required": request.args.get("quantity_required", ""),
    }

    inventory = get_leftovers_sorted()
    # Leftovers >= 100 mm: show as suggestion for next plan (fits requirements <= that size)
    inventory_suggestions = [r for r in inventory if float(r.get("length", 0)) >= SCRAP_SAVE_THRESHOLD_MM]

    return render_template(
        "index.html",
        result=result,
        multi_result=multi_result,
        inventory=inventory,
        inventory_suggestions=inventory_suggestions,
        prefill=prefill,
        standard_raw_mm=STANDARD_RAW_LENGTH_MM,
        scrap_threshold_mm=SCRAP_SAVE_THRESHOLD_MM,
    )


if __name__ == "__main__":
    init_db()
    # Use 5001 if 5000 is taken (e.g. by macOS AirPlay Receiver)
    app.run(host="127.0.0.1", port=5001, debug=False)
