"""
Samruddhi Enterprises - SS Steel Pipe Cutting Instruction System
All lengths in millimeters. "Cut 7 × 5000 mm → 65000 mm remaining." One raw pipe = one length (mm).
Uses db.py for storage: SQLite locally, Turso on Vercel when env vars are set.

Extended: Multi-size cutting plan (FFD bin packing) with standard raw length 3600 mm.
"""

import os
from flask import Flask, redirect, render_template, request

import db

app = Flask(__name__)

# Standard raw pipe length for multi-size mode (mm). Fixed, no user input.
STANDARD_RAW_LENGTH_MM = 3600.0

# Kerf loss: fixed waste per physical cut (mm). Applied PER CUT in all calculations.
KERF_MM = 3.0

# Scrap below this length is not saved to inventory. Only pieces >= this are stored.
SCRAP_SAVE_THRESHOLD_MM = 100.0

# Future-usability threshold for leftovers:
# A remaining length is treated as USABLE if it is large enough that,
# on its own or in combination with a similar future leftover, the factory
# can realistically obtain a 700–800 mm piece.
#
# Heuristic:
# - Two pieces of ~350 mm can later combine into ~700 mm (ignoring a small kerf allowance),
#   so we treat any remainder >= 350 mm as "future-usable".
SCRAP_USABLE_MIN_MM = 350.0

# Client rule: the LAST pipe in the plan must have scrap ≤ 75 mm.
# If last pipe would have > 75 mm scrap, we move cuts to new pipe(s) until last ≤ 75 mm.
LAST_PIPE_SCRAP_MAX_MM = 75.0


def classify_scrap_mm(scrap_mm: float) -> str:
    """
    Classify remaining length for future usability (700–800 mm range).

    - USABLE:     scrap_mm >= SCRAP_USABLE_MIN_MM
                  (can directly yield, or be combined to yield, a 700–800 mm piece)
    - NOT USABLE: scrap_mm < SCRAP_USABLE_MIN_MM
                  (too short to be realistically useful for 700–800 mm later)
    """
    if scrap_mm >= SCRAP_USABLE_MIN_MM:
        return "USABLE"
    return "NOT USABLE"

# Not used here; db module handles storage
get_leftovers_sorted = db.get_leftovers_sorted
delete_leftover = db.delete_leftover
insert_leftover = db.insert_leftover
delete_leftovers_batch = db.delete_leftovers_batch
insert_leftovers_batch = db.insert_leftovers_batch
clear_all_leftovers = db.clear_all_leftovers
init_db = db.init_db


def run_multi_size_plan(cut_requirements, leftovers=None):
    """
    Multi-size cutting plan: use leftovers first (largest first), then raw 3600 mm pipes.
    Kerf: 3 mm PER CUT. Client rule: last pipe scrap must be ≤ 75 mm (move cuts to new pipes if needed).

    cut_requirements: list of (length_mm, quantity) e.g. [(868, 3)]
    leftovers: optional list of dicts with "id", "length" (from DB, sorted largest first).
    Returns list of pipes with pipe_label, cuts, num_cuts, kerf_mm, used, scrap, scrap_class,
    is_leftover, leftover_id (for DB: delete used leftovers, insert new scrap).
    """
    if not cut_requirements:
        return {"pipes": [], "total_pipes": 0, "total_used": 0, "total_scrap": 0, "total_kerf": 0, "last_pipe_over_limit": False}

    # 1. Expand to flat list and sort descending (FFD)
    flat = []
    for length_mm, qty in cut_requirements:
        L = round(float(length_mm), 2)
        n = int(qty)
        if L <= 0 or n <= 0:
            continue
        flat.extend([L] * n)
    flat.sort(reverse=True)

    if not flat:
        return {"pipes": [], "total_pipes": 0, "total_used": 0, "total_scrap": 0, "total_kerf": 0, "last_pipe_over_limit": False}

    leftovers = leftovers or []
    # Helper: future-usability (only for raw 3600 mm pipes; leftovers are "use first", pack tight).
    def _is_remaining_usable(remaining: float) -> bool:
        return remaining >= SCRAP_USABLE_MIN_MM

    # 2. Initialize pipes: leftovers first (largest first), then we add raw pipes as needed.
    #    Each pipe: remaining, cuts, is_leftover, leftover_id, capacity (for label).
    pipes = []
    for row in leftovers:
        length_mm = round(float(row.get("length", 0)), 2)
        if length_mm <= 0:
            continue
        pipes.append({
            "remaining": length_mm,
            "cuts": [],
            "is_leftover": True,
            "leftover_id": row.get("id"),
            "capacity": length_mm,
        })

    # 3. Place each cut: MUST try leftover pipes first (use-first), then raw pipes. Best-fit within each group.
    for cut in flat:
        needed = cut + KERF_MM
        best_pipe = None
        best_remaining_after = None
        # Prefer leftovers: only consider raw pipes if no leftover can take this cut.
        candidates = [p for p in pipes if p["remaining"] >= needed]
        leftover_candidates = [p for p in candidates if p["is_leftover"]]
        # Try leftovers first; if any fit, choose best-fit among leftovers only.
        search_list = leftover_candidates if leftover_candidates else candidates

        for p in search_list:
            if p["remaining"] < needed:
                continue

            rem_before = p["remaining"]
            rem_after = rem_before - needed

            # Future-usability guard only for raw pipes (3600 mm). Leftovers: pack as much as fits.
            if not p["is_leftover"] and p["cuts"]:
                before_usable = _is_remaining_usable(rem_before)
                after_usable = _is_remaining_usable(rem_after)
                if before_usable and not after_usable:
                    continue

            if best_remaining_after is None or rem_after < best_remaining_after:
                best_remaining_after = rem_after
                best_pipe = p

        if best_pipe is not None:
            best_pipe["cuts"].append(cut)
            best_pipe["remaining"] = round(best_remaining_after, 2)
        else:
            # No leftover and no raw pipe can take this cut — open a new raw pipe (3600 mm).
            pipes.append({
                "remaining": round(STANDARD_RAW_LENGTH_MM - needed, 2),
                "cuts": [cut],
                "is_leftover": False,
                "leftover_id": None,
                "capacity": STANDARD_RAW_LENGTH_MM,
            })

    # 4. Client rule: last pipe scrap must be ≤ 75 mm. Move smallest cuts out, then PACK them
    # into as few new 3600 mm pipes as possible (avoids creating one pipe per cut = slow output).
    if pipes and pipes[-1]["remaining"] > LAST_PIPE_SCRAP_MAX_MM and pipes[-1]["cuts"]:
        last = pipes[-1]
        cuts_sorted = sorted(last["cuts"])  # ascending: smallest first
        # Find how many smallest cuts to move so that last pipe remaining ≤ 75.
        move_count = 0
        for i in range(1, len(cuts_sorted) + 1):
            kept = cuts_sorted[i:]
            if not kept:
                remaining = last["capacity"]
            else:
                used = sum(kept) + len(kept) * KERF_MM
                remaining = last["capacity"] - used
            if remaining <= LAST_PIPE_SCRAP_MAX_MM:
                move_count = i
                break
        if move_count > 0:
            moved_cuts = cuts_sorted[:move_count]
            last["cuts"] = cuts_sorted[move_count:]
            last_used = sum(last["cuts"]) + len(last["cuts"]) * KERF_MM if last["cuts"] else 0
            last["remaining"] = round(last["capacity"] - last_used, 2)
            # Pack moved_cuts into as few new 3600 mm pipes as possible (FFD with kerf).
            new_pipes = []
            for c in reversed(moved_cuts):  # descending for first-fit
                needed = c + KERF_MM
                placed = False
                for np in new_pipes:
                    if np["remaining"] >= needed:
                        np["cuts"].append(c)
                        np["remaining"] = round(np["remaining"] - needed, 2)
                        placed = True
                        break
                if not placed:
                    new_pipes.append({
                        "remaining": round(STANDARD_RAW_LENGTH_MM - needed, 2),
                        "cuts": [c],
                        "is_leftover": False,
                        "leftover_id": None,
                        "capacity": STANDARD_RAW_LENGTH_MM,
                    })
            pipes = pipes[:-1] + ([pipes[-1]] if pipes[-1]["cuts"] else []) + new_pipes
    # Drop raw pipes with no cuts; keep leftover pipes even with 0 cuts so the plan shows "use leftover first".
    pipes = [p for p in pipes if p["cuts"] or p.get("is_leftover")]

    # 5. Build output: pipe_number, pipe_label, cuts, num_cuts, kerf_mm, used, scrap, scrap_class, is_leftover, leftover_id
    out = []
    total_used = 0
    total_scrap = 0
    total_kerf = 0
    last_pipe_over_limit = False
    for i, p in enumerate(pipes):
        num_cuts = len(p["cuts"])
        pieces_only = round(sum(p["cuts"]), 2)
        kerf_mm = round(num_cuts * KERF_MM, 2)
        used = round(pieces_only + kerf_mm, 2)
        scrap = round(p["remaining"], 2)
        total_used += used
        total_scrap += scrap
        total_kerf += kerf_mm
        if p["is_leftover"]:
            pipe_label = "Leftover {:.0f} mm".format(p["capacity"])
        else:
            pipe_label = "Raw pipe (3600 mm)"
        if i == len(pipes) - 1 and scrap > LAST_PIPE_SCRAP_MAX_MM:
            last_pipe_over_limit = True
        out.append({
            "pipe_number": i + 1,
            "pipe_label": pipe_label,
            "cuts": p["cuts"],
            "num_cuts": num_cuts,
            "kerf_mm": kerf_mm,
            "used": used,
            "scrap": scrap,
            "scrap_class": classify_scrap_mm(scrap),
            "is_leftover": p["is_leftover"],
            "leftover_id": p.get("leftover_id"),
        })

    return {
        "pipes": out,
        "total_pipes": len(out),
        "total_used": round(total_used, 2),
        "total_scrap": round(total_scrap, 2),
        "total_kerf": round(total_kerf, 2),
        "raw_length": STANDARD_RAW_LENGTH_MM,
        "last_pipe_over_limit": last_pipe_over_limit,
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
        if request.form.get("clear_inventory"):
            clear_all_leftovers()
            return redirect(request.url)
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
                # Use stored leftovers first (largest first); then raw 3600 mm pipes.
                inventory = get_leftovers_sorted()
                multi_result = run_multi_size_plan(pairs, leftovers=inventory)
                # Update DB in one batch: remove used leftovers; add new scrap (fewer round-trips)
                if multi_result and multi_result.get("pipes"):
                    ids_to_delete = []
                    scrap_to_insert = []
                    for p in multi_result["pipes"]:
                        scrap = p.get("scrap", 0)
                        if p.get("leftover_id") and p.get("num_cuts", 0) > 0:
                            ids_to_delete.append(p["leftover_id"])
                            if scrap >= SCRAP_SAVE_THRESHOLD_MM:
                                scrap_to_insert.append(scrap)
                        elif not p.get("leftover_id") and scrap >= SCRAP_SAVE_THRESHOLD_MM:
                            scrap_to_insert.append(scrap)
                    delete_leftovers_batch(ids_to_delete)
                    insert_leftovers_batch(scrap_to_insert)
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
    # Use 5002 (5000/5001 often taken by macOS AirPlay or previous run)
    app.run(host="127.0.0.1", port=5002, debug=False)
