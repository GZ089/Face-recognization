import io
import base64
import logging
import time

import numpy as np
import cv2
from PIL import Image
from flask import Flask, request, jsonify, render_template

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

CONFIDENCE_THRESHOLD = 65

# ═══════════════════════════════════════════════════════════
#  In-memory model cache (avoids retraining on every verify)
# ═══════════════════════════════════════════════════════════
_model_cache = {
    "recognizer": None,
    "labels": {},
    "signature": None,      # hash of the training data that built the model
    "trained_on": 0,
}

print("=" * 60)
print("  Face Recognition Gate — Adaptive Enrollment Server")
print(f"  OpenCV:        {cv2.__version__}")
print(f"  LBPH available: {hasattr(cv2.face, 'LBPHFaceRecognizer_create')}")
print("=" * 60)


# ─── Face detector ────────────────────────────────────────
_face_cascade = None


def get_face_cascade():
    global _face_cascade
    if _face_cascade is None:
        path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        _face_cascade = cv2.CascadeClassifier(path)
        print(f"[Cascade] loaded (empty={_face_cascade.empty()})")
    return _face_cascade


# ─── Image helpers ────────────────────────────────────────
def decode_base64_image(b64_string):
    if "," in b64_string:
        b64_string = b64_string.split(",", 1)[1]
    img_bytes = base64.b64decode(b64_string)
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    return np.array(img)


def prepare_gray(rgb):
    if rgb is None or rgb.size == 0:
        raise ValueError("Empty image")
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    return np.ascontiguousarray(gray.astype(np.uint8))


def detect_faces(gray_img):
    cascade = get_face_cascade()
    if gray_img is None or gray_img.size == 0:
        return []
    if len(gray_img.shape) == 3:
        gray_img = cv2.cvtColor(gray_img, cv2.COLOR_BGR2GRAY)
    if gray_img.dtype != np.uint8:
        gray_img = gray_img.astype(np.uint8)
    gray_img = np.ascontiguousarray(gray_img)
    gray_img = cv2.equalizeHist(gray_img)
    try:
        faces = cascade.detectMultiScale(
            gray_img, scaleFactor=1.05, minNeighbors=4,
            minSize=(40, 40), flags=cv2.CASCADE_SCALE_IMAGE
        )
        if len(faces) == 0:
            faces = cascade.detectMultiScale(
                gray_img, scaleFactor=1.03, minNeighbors=3,
                minSize=(30, 30), flags=cv2.CASCADE_SCALE_IMAGE
            )
    except cv2.error as e:
        print("[detect_faces] error:", e)
        return []
    return faces


def extract_face_200(gray, box):
    x, y, w, h = box
    face = gray[y:y+h, x:x+w]
    if face.size == 0:
        return None
    face = cv2.resize(face, (200, 200))
    return np.ascontiguousarray(face.astype(np.uint8))


# ═══════════════════════════════════════════════════════════
#  Quality check (blur, brightness, size, centering)
# ═══════════════════════════════════════════════════════════
def quality_check(gray_face_200):
    """
    Run quality metrics on a 200x200 grayscale face crop.
    Returns dict with per-metric pass/fail and detail values.
    """
    result = {
        "ok": True,
        "issues": [],
        "sharpness": 0.0,
        "brightness": 0.0,
    }

    if gray_face_200 is None or gray_face_200.size == 0:
        result["ok"] = False
        result["issues"].append("empty")
        return result

    # ── Blur: variance of Laplacian (higher = sharper) ──
    lap_var = cv2.Laplacian(gray_face_200, cv2.CV_64F).var()
    result["sharpness"] = round(float(lap_var), 1)
    if lap_var < 60:      # too blurry
        result["ok"] = False
        result["issues"].append("blurry")

    # ── Brightness ──
    mean_brightness = float(gray_face_200.mean())
    result["brightness"] = round(mean_brightness, 1)
    if mean_brightness < 50:
        result["ok"] = False
        result["issues"].append("too_dark")
    elif mean_brightness > 210:
        result["ok"] = False
        result["issues"].append("too_bright")

    # ── Contrast: std-dev of pixel values ──
    contrast = float(gray_face_200.std())
    result["contrast"] = round(contrast, 1)
    if contrast < 25:
        result["ok"] = False
        result["issues"].append("low_contrast")

    return result


# ═══════════════════════════════════════════════════════════
#  Model cache helpers
# ═══════════════════════════════════════════════════════════
def compute_samples_signature(samples):
    """
    Cheap deterministic signature of training samples.
    Changes only when the set of (name, imageCount) changes.
    """
    parts = []
    for p in sorted(samples, key=lambda x: x.get("name", "")):
        name = p.get("name", "")
        imgs = p.get("images", [])
        # Use length + hash of first+last image to detect edits
        first = imgs[0][:32] if imgs else ""
        last = imgs[-1][:32] if imgs else ""
        parts.append(f"{name}:{len(imgs)}:{hash(first+last)}")
    return "|".join(parts)


def train_model_from_samples(samples):
    """
    Build an LBPH recognizer from client-supplied samples.
    Returns (recognizer, labels, count) or (None, {}, 0) on failure.
    """
    faces_list, ids_list, labels = [], [], {}
    next_id = 0

    for person in samples:
        name = person.get("name")
        images = person.get("images", [])
        if not name or not images:
            continue
        labels[next_id] = name
        for b64 in images:
            try:
                if "," in b64:
                    b64 = b64.split(",", 1)[1]
                raw = base64.b64decode(b64)
                arr = np.frombuffer(raw, dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
                if img is None:
                    continue
                img = cv2.resize(img, (200, 200))
                img = np.ascontiguousarray(img.astype(np.uint8))
                faces_list.append(img)
                ids_list.append(next_id)
            except Exception as e:
                print(f"[TRAIN] bad sample for {name}: {e}")
        next_id += 1

    if not faces_list:
        return None, {}, 0

    try:
        recognizer = cv2.face.LBPHFaceRecognizer_create()
        recognizer.train(faces_list, np.array(ids_list))
        return recognizer, labels, len(faces_list)
    except cv2.error as e:
        print("[TRAIN] failed:", e)
        return None, {}, 0


# ─── Routes ───────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/enroll")
def enroll_page():
    return render_template("enroll.html")

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "success": True,
        "opencv": cv2.__version__,
        "lbph": hasattr(cv2.face, "LBPHFaceRecognizer_create"),
    })


@app.route("/api/crop", methods=["POST"])
def crop():
    """
    Client already cropped the face using MediaPipe.
    Normalize to 200x200 grayscale PNG + run quality check.
    """
    data = request.get_json() or {}
    image = data.get("image", "")
    if not image:
        return jsonify({"success": False, "error": "Image required"}), 400

    try:
        rgb = decode_base64_image(image)
        gray = prepare_gray(rgb)
    except Exception as e:
        return jsonify({"success": False, "error": f"Bad image: {e}"}), 400

    try:
        face_200 = cv2.resize(gray, (200, 200))
        face_200 = np.ascontiguousarray(face_200.astype(np.uint8))
    except cv2.error as e:
        return jsonify({"success": False, "error": f"Resize failed: {e}"}), 500

    # Quality check
    qc = quality_check(face_200)
    if not qc["ok"]:
        return jsonify({
            "success": False,
            "error": "Quality check failed",
            "quality": qc,
        }), 400

    ok, buf = cv2.imencode(".png", face_200)
    if not ok:
        return jsonify({"success": False, "error": "PNG encode failed"}), 500

    crop_b64 = base64.b64encode(buf.tobytes()).decode()
    return jsonify({
        "success": True,
        "crop": crop_b64,
        "quality": qc,
    })


@app.route("/api/quality", methods=["POST"])
def quality_only():
    """Just run quality check without saving."""
    data = request.get_json() or {}
    image = data.get("image", "")
    if not image:
        return jsonify({"success": False, "error": "Image required"}), 400
    try:
        rgb = decode_base64_image(image)
        gray = prepare_gray(rgb)
        face_200 = cv2.resize(gray, (200, 200))
        face_200 = np.ascontiguousarray(face_200.astype(np.uint8))
    except Exception as e:
        return jsonify({"success": False, "error": f"Bad image: {e}"}), 400
    return jsonify({"success": True, "quality": quality_check(face_200)})


@app.route("/api/recognize", methods=["POST"])
def recognize():
    """
    Train LBPH only if the sample set changed (cached in memory).
    """
    data = request.get_json() or {}
    image = data.get("image", "")
    samples = data.get("samples", [])

    if not image:
        return jsonify({"success": False, "error": "Image required"}), 400

    try:
        rgb = decode_base64_image(image)
        gray = prepare_gray(rgb)
    except Exception as e:
        return jsonify({"success": False, "error": f"Bad image: {e}"}), 400

    # No samples → just detect
    if not samples:
        faces = detect_faces(gray)
        results = []
        for (x, y, w, h) in faces:
            results.append({
                "name": "Unknown", "confidence": 0.0,
                "box": {"top": int(y), "right": int(x+w), "bottom": int(y+h), "left": int(x)},
                "marked": False, "authorized": False,
            })
        return jsonify({"success": True, "results": results, "trained_on": 0, "cached": False})

    # Check if we can use cached model
    signature = compute_samples_signature(samples)
    global _model_cache

    if _model_cache["signature"] != signature or _model_cache["recognizer"] is None:
        # Retrain
        t0 = time.time()
        recognizer, labels, count = train_model_from_samples(samples)
        elapsed = time.time() - t0
        if recognizer is None:
            return jsonify({"success": False, "error": "Training failed"}), 500
        _model_cache["recognizer"] = recognizer
        _model_cache["labels"] = labels
        _model_cache["signature"] = signature
        _model_cache["trained_on"] = count
        print(f"[RECOGNIZE] Retrained model ({count} samples, {elapsed*1000:.0f}ms)")
        cached = False
    else:
        recognizer = _model_cache["recognizer"]
        labels = _model_cache["labels"]
        cached = True
        print(f"[RECOGNIZE] Using cached model ({_model_cache['trained_on']} samples)")

    faces = detect_faces(gray)
    results = []

    for (x, y, w, h) in faces:
        face_img = extract_face_200(gray, (x, y, w, h))
        if face_img is None:
            continue
        try:
            label_id, distance = recognizer.predict(face_img)
        except cv2.error as e:
            print("[RECOGNIZE] predict error:", e)
            continue

        confidence = max(0.0, 1.0 - (distance / 100.0))
        authorized = False
        if distance < CONFIDENCE_THRESHOLD and label_id in labels:
            name = labels[label_id]
            authorized = True
        else:
            name = "Unknown"

        results.append({
            "name": name,
            "confidence": round(confidence, 3),
            "distance": round(float(distance), 2),
            "box": {"top": int(y), "right": int(x+w), "bottom": int(y+h), "left": int(x)},
            "marked": authorized,
            "authorized": authorized,
        })

    return jsonify({
        "success": True,
        "results": results,
        "trained_on": _model_cache["trained_on"],
        "cached": cached,
    })


@app.route("/api/invalidate_model", methods=["POST"])
def invalidate_model():
    """Force model retrain on next recognize call."""
    global _model_cache
    _model_cache["signature"] = None
    return jsonify({"success": True})


if __name__ == "__main__":
    print("Open http://localhost:5000 in your browser")
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)