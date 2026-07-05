"""
main.py — Lumen integration loop.

Live system: webcam → blink detection → rolling baseline → risk engine
→ state badge + console log → serial → ESP32-S3 dome (if connected).

Modes:
  Auto      — risk engine decides state. Requires face. No face → NO SIGNAL.
  Override  — user forces CALM / ATTENTION / BREAK via keys 1/2/3.
              Bypasses sensing. Useful for demo and user research.

DEMO_MODE = True:  baseline samples every 1s (settles in 30s).
DEMO_MODE = False: baseline samples every 60s (real mode, settles in 30 min).

Hardware is optional. If SERIAL_PORT is None or the port can't be opened,
the loop runs exactly as before: webcam window only, no dome. Find your
port with `ls /dev/tty.*` (macOS/Linux) or the Arduino IDE's port list
(Windows: "COM3" etc.), then set SERIAL_PORT below.

Keyboard:
  q   quit
  r   full session reset (blink counter, baseline, risk timer, override)
  d   toggle DEMO / REAL mode
  0   clear override, return to auto
  1   force CALM
  2   force ATTENTION
  3   force BREAK
"""

import cv2
import time
from collections import deque
import mediapipe as mp
import serial

from core.blink_detector import calculate_ear, LEFT_EYE, RIGHT_EYE
from core.baseline import BaselineEngine
from core.risk_engine import RiskEngine
from core.state import DomeState


# ---------- Config ----------
DEMO_MODE = True
EAR_THRESHOLD = 0.21
CONSEC_FRAMES = 2
CONSEC_OPEN = 3
WINDOW_SECONDS = 60
FACE_LOST_THRESHOLD = 30.0      # seconds without face → pin break clock to "now"

BASELINE_INTERVAL = 1.0 if DEMO_MODE else 60.0

FORCE_STATE = None              # None = auto. Set via keys 1/2/3.

# ---------- Dome (serial) ----------
SERIAL_PORT = None              # e.g. "/dev/tty.usbmodem1101" or "COM5". None = no hardware.
SERIAL_BAUD = 115200

DOME_COMMANDS = {
    DomeState.CALM:      b"C",
    DomeState.ATTENTION: b"A",
    DomeState.BREAK:     b"B",
}

dome_serial = None
if SERIAL_PORT:
    try:
        dome_serial = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=0)
        time.sleep(2)  # ESP32 resets on serial connect; give it time to boot
        print(f"[ 0.0s] Dome connected on {SERIAL_PORT}")
    except (serial.SerialException, OSError) as e:
        print(f"[ 0.0s] Could not open {SERIAL_PORT} ({e}), running without dome")
        dome_serial = None


def send_to_dome(state):
    """Write a single state command to the dome. No-op if not connected."""
    if dome_serial is None:
        return
    cmd = DOME_COMMANDS.get(state)
    if cmd is None:
        return
    try:
        dome_serial.write(cmd)
    except serial.SerialException:
        pass  # dome dropped mid-session, don't crash the vision loop over it


# ---------- Colors ----------
STATE_COLORS_BGR = {
    DomeState.CALM:      (180, 140,   0),
    DomeState.ATTENTION: (  0, 165, 255),
    DomeState.BREAK:     (  0,   0, 220),
}
NO_SIGNAL_COLOR = (80, 80, 80)

STATE_FROM_STR = {
    "CALM":      DomeState.CALM,
    "ATTENTION": DomeState.ATTENTION,
    "BREAK":     DomeState.BREAK,
}


# ---------- Engines & state ----------
session_start = time.time()
baseline = BaselineEngine()
risk_engine = RiskEngine(start_time=session_start)

consec_below = 0
consec_above = 0
is_closed = False
blink_count = 0
blink_times = deque()

last_baseline_update = session_start
last_face_seen = session_start

avg_ear = None
previous_label = "CALM"


# ---------- Glass panel helpers ----------
def _rounded_rect_filled(frame, x, y, w, h, color, radius=10):
    """Filled rounded rectangle drawn directly onto frame."""
    cv2.rectangle(frame, (x + radius, y), (x + w - radius, y + h), color, -1)
    cv2.rectangle(frame, (x, y + radius), (x + w, y + h - radius), color, -1)
    cv2.circle(frame, (x + radius, y + radius), radius, color, -1)
    cv2.circle(frame, (x + w - radius, y + radius), radius, color, -1)
    cv2.circle(frame, (x + radius, y + h - radius), radius, color, -1)
    cv2.circle(frame, (x + w - radius, y + h - radius), radius, color, -1)


def draw_glass_panel(frame, x, y, w, h, alpha=0.55, color=(20, 20, 20), radius=10):
    """Semi-transparent dark panel for text legibility."""
    overlay = frame.copy()
    _rounded_rect_filled(overlay, x, y, w, h, color, radius)
    cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, frame)


# ---------- MediaPipe + capture ----------
mp_face_mesh = mp.solutions.face_mesh
face_mesh = mp_face_mesh.FaceMesh(
    max_num_faces=1,
    refine_landmarks=True,
    min_detection_confidence=0.5,
    min_tracking_confidence=0.5,
)
capture = cv2.VideoCapture(0)

print(f"[ 0.0s] Lumen started. DEMO_MODE={DEMO_MODE}, interval={BASELINE_INTERVAL}s")
print(f"[ 0.0s] state: CALM (initial, auto)")


# ---------- Main loop ----------
while capture.isOpened():
    ok, frame = capture.read()
    if not ok:
        break

    now = time.time()
    elapsed = now - session_start

    key = cv2.waitKey(1) & 0xFF
    if key == ord('q'):
        break
    if key == ord('r'):
        blink_count = 0
        blink_times.clear()
        baseline = BaselineEngine()
        risk_engine = RiskEngine(start_time=now)
        last_baseline_update = now
        last_face_seen = now
        consec_below = 0
        consec_above = 0
        is_closed = False
        session_start = now
        previous_label = "CALM"
        FORCE_STATE = None
        print(f"[ 0.0s] === session reset ===")
    if key == ord('d'):
        DEMO_MODE = not DEMO_MODE
        BASELINE_INTERVAL = 1.0 if DEMO_MODE else 60.0
        print(f"[{elapsed:6.1f}s] mode: {'DEMO' if DEMO_MODE else 'REAL'} "
              f"(interval={BASELINE_INTERVAL}s)")
    if key == ord('0'):
        FORCE_STATE = None
        print(f"[{elapsed:6.1f}s] override cleared → auto")
    if key == ord('1'):
        FORCE_STATE = DomeState.CALM
        print(f"[{elapsed:6.1f}s] override → CALM")
    if key == ord('2'):
        FORCE_STATE = DomeState.ATTENTION
        print(f"[{elapsed:6.1f}s] override → ATTENTION")
    if key == ord('3'):
        FORCE_STATE = DomeState.BREAK
        print(f"[{elapsed:6.1f}s] override → BREAK")

    # Prune expired blinks from the rolling 60s window
    while blink_times and now - blink_times[0] > WINDOW_SECONDS:
        blink_times.popleft()
    blinks_per_min = len(blink_times)

    # --- Face mesh ---
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    results = face_mesh.process(rgb)
    face_present = results.multi_face_landmarks is not None

    if face_present:
        last_face_seen = now

        landmarks = results.multi_face_landmarks[0].landmark
        h, w = frame.shape[:2]

        for eye_indices in [LEFT_EYE, RIGHT_EYE]:
            for idx in eye_indices:
                lm = landmarks[idx]
                cv2.circle(frame, (int(lm.x * w), int(lm.y * h)), 2, (0, 255, 0), -1)

        left_ear = calculate_ear(landmarks, LEFT_EYE, w, h)
        right_ear = calculate_ear(landmarks, RIGHT_EYE, w, h)
        avg_ear = (left_ear + right_ear) / 2.0
        both_closed = (left_ear < EAR_THRESHOLD) and (right_ear < EAR_THRESHOLD)

        # Blink detection state machine
        if both_closed:
            consec_above = 0
            consec_below += 1
            if consec_below >= CONSEC_FRAMES:
                is_closed = True
        else:
            consec_above += 1
            if consec_above >= CONSEC_OPEN:
                if is_closed:
                    blink_count += 1
                    blink_times.append(now)
                is_closed = False
                consec_below = 0
    else:
        # Face absent — if absence exceeds threshold, KEEP pinning the break clock
        # to "now" every frame. Focus clock stays at 0 throughout absence.
        # No flag — calling mark_break repeatedly is harmless (idempotent).
        if (now - last_face_seen) >= FACE_LOST_THRESHOLD:
            risk_engine.mark_break(now)
        avg_ear = None

    # --- Feed baseline ONLY when face is present ---
    # When no face, we have no data, so we add nothing. Deque preserves
    # its previous values. When face returns, baseline picks up where it left off.
    if face_present and (now - last_baseline_update) >= BASELINE_INTERVAL:
        baseline.add_sample(blinks_per_min)
        last_baseline_update = now

    # --- Compute risk for telemetry display ---
    baseline_state = baseline.get_state()
    risk = risk_engine.get_risk(baseline_state, now)

    # --- Determine what to display ---
    # Priority: override > face-present auto > no signal
    if FORCE_STATE is not None:
        state_label = f"{FORCE_STATE.name} [OVR]"
        badge_color = STATE_COLORS_BGR[FORCE_STATE]
    elif face_present:
        auto_state = STATE_FROM_STR[risk["recommended_state"]]
        state_label = auto_state.name
        badge_color = STATE_COLORS_BGR[auto_state]
    else:
        state_label = "NO SIGNAL"
        badge_color = NO_SIGNAL_COLOR

    # Log only on change
    if state_label != previous_label:
        if FORCE_STATE is not None:
            print(f"[{elapsed:6.1f}s] state: {previous_label} → {state_label}")
            send_to_dome(FORCE_STATE)
        elif face_present:
            print(f"[{elapsed:6.1f}s] state: {previous_label} → {state_label} "
                  f"(risk={risk['risk_score']:.2f}, "
                  f"blink={risk['blink_risk']:.2f}, focus={risk['focus_risk']:.2f})")
            send_to_dome(auto_state)
        else:
            print(f"[{elapsed:6.1f}s] state: {previous_label} → {state_label} (face lost)")
            # no dome command, NO SIGNAL isn't a dome state, just holds the last one
        previous_label = state_label

    # --- Overlay ---
    frame_h, frame_w = frame.shape[:2]

    # Glass panels first (drawn UNDER text)
    draw_glass_panel(frame, x=5, y=5, w=215, h=95)
    badge_w, badge_h = 240, 60
    bx = frame_w - badge_w - 10
    by = 10
    draw_glass_panel(frame, x=bx, y=by + badge_h + 8, w=badge_w, h=152)

    # State badge — opaque rounded, color-coded
    _rounded_rect_filled(frame, bx, by, badge_w, badge_h, badge_color)
    label_font_scale = 0.85 if len(state_label) > 8 else 1.2
    cv2.putText(frame, state_label, (bx + 14, by + 44),
                cv2.FONT_HERSHEY_SIMPLEX, label_font_scale, (255, 255, 255), 2)

    # Left column: blink stats
    line_h = 28
    if avg_ear is not None:
        cv2.putText(frame, f"EAR:    {avg_ear:.3f}", (16, 32),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (120, 255, 120), 2)
    else:
        cv2.putText(frame, "EAR:    --", (16, 32),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (140, 140, 140), 2)
    cv2.putText(frame, f"Blinks: {blink_count}", (16, 32 + line_h),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (120, 255, 120), 2)
    cv2.putText(frame, f"Rate:   {blinks_per_min}/min", (16, 32 + 2 * line_h),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (100, 220, 255), 2)

    # Right column: risk stats
    rx = bx + 14
    ry = by + badge_h + 32
    floor_tag = " *" if risk["floor_active"] else ""
    cv2.putText(frame, f"Risk:  {risk['risk_score']:.2f}", (rx, ry),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    cv2.putText(frame, f"Ref:   {risk['reference']:.1f}{floor_tag}",
                (rx, ry + line_h),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    cv2.putText(frame, f"Focus: {risk['minutes_without_break']:.1f}m",
                (rx, ry + 2 * line_h),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    cv2.putText(frame, f"Mode:  {'DEMO' if DEMO_MODE else 'REAL'}",
                (rx, ry + 3 * line_h),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (220, 220, 220), 1)
    cv2.putText(frame, f"T+{elapsed:.0f}s",
                (rx, ry + 4 * line_h),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (220, 220, 220), 1)

    cv2.imshow("Lumen — live", frame)

capture.release()
cv2.destroyAllWindows()
if dome_serial is not None:
    dome_serial.close()
print(f"[{time.time() - session_start:6.1f}s] Lumen stopped.")