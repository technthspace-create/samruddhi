"""
Samruddhi Enterprises - SS Steel Pipe Cutting Instruction System
All lengths in millimeters. "Cut 7 × 5000 mm → 65000 mm remaining." One raw pipe = one length (mm).
Uses db.py for storage: SQLite locally, Turso on Vercel when env vars are set.
"""

import os
from flask import Flask, render_template, request

import db

app = Flask(__name__)

# Not used here; db module handles storage
get_leftovers_sorted = db.get_leftovers_sorted
delete_leftover = db.delete_leftover
insert_leftover = db.insert_leftover
init_db = db.init_db


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

    def next_source():
        """Next usable leftover (largest first) or one new raw pipe."""
        nonlocal leftover_index
        while leftover_index < len(leftovers):
            row = leftovers[leftover_index]
            leftover_index += 1
            if row["length"] >= cut_length:
                return row["length"], "Leftover", row["id"], "Leftover ({:.2f} mm)".format(row["length"])
        return raw_material_length, "Raw material", None, "Raw pipe ({:.2f} mm)".format(raw_material_length)

    available_length, current_source, current_leftover_id, current_source_label = next_source()
    source_initial_length = available_length

    while quantity_required > 0 and cut_length > 0:
        if available_length >= cut_length:
            # Cut one piece
            pieces_from_current += 1
            quantity_required -= 1
            available_length = round(available_length - cut_length, 2)
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
            if remaining > 0:
                scrap_to_save.append(remaining)
            if current_leftover_id is not None:
                used_leftover_ids.append(current_leftover_id)
                used_leftover = True
            pieces_from_current = 0
            available_length, current_source, current_leftover_id, current_source_label = next_source()
            source_initial_length = available_length
            if quantity_required > 0 and available_length < cut_length:
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
        if remaining > 0:
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
    material_used = round(total_pieces * cut_length, 2)

    return {
        "pieces_produced": total_pieces,
        "material_used": material_used,
        "scrap_saved_list": scrap_to_save,
        "used_leftover": used_leftover,
        "segments": segments,
        "suggested_raw": suggested_raw,
    }


@app.route("/", methods=["GET", "POST"])
def index():
    init_db()  # ensure table exists (needed on Vercel; no-op if already exists)
    result = None
    if request.method == "POST":
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
    return render_template(
        "index.html",
        result=result,
        inventory=inventory,
        prefill=prefill,
    )


if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5000, debug=False)
