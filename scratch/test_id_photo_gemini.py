import os
import sys
import time
import warnings
warnings.filterwarnings('ignore')

import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.conf import settings
import google.generativeai as genai
from PIL import Image, ImageDraw

# ── Build a synthetic "portrait" test image (not a real face, just to validate the API call) ──
img = Image.new('RGB', (600, 800), color=(200, 180, 160))
draw = ImageDraw.Draw(img)
# head
draw.ellipse((175, 150, 425, 450), fill=(230, 200, 170))
# hair
draw.ellipse((160, 100, 440, 300), fill=(60, 40, 30))
draw.rectangle((175, 200, 425, 320), fill=(230, 200, 170))
draw.ellipse((160, 100, 440, 260), fill=(60, 40, 30))
# eyes
draw.ellipse((230, 270, 260, 290), fill=(30, 30, 30))
draw.ellipse((340, 270, 370, 290), fill=(30, 30, 30))
# mouth
draw.arc((250, 330, 350, 400), start=20, end=160, fill=(150, 60, 60), width=4)
# shoulders/shirt (red shirt, to test outfit change)
draw.rectangle((100, 450, 500, 800), fill=(180, 40, 40))

img.save('scratch/test_input.png')
print(f"[Test] Built synthetic test image: scratch/test_input.png, size={img.size}")

api_key = getattr(settings, 'GEMINI_API_KEY', '').strip()
if not api_key:
    print("[Test] GEMINI_API_KEY not configured")
    sys.exit(1)

genai.configure(api_key=api_key)

MERGE_OUTFIT_PROMPT = (
    "Keep 100% of the original face, facial structure, expression, eyes, skin tone, "
    "hairstyle, and facial lighting exactly as in the original image. Absolutely do not "
    "modify the face, do not recreate the face, do not blur, and do not alter any facial "
    "details in any way. ONLY CHANGE THE OUTFIT TO MATCH THE REFERENCE AND MAKE IT AN ID "
    "PHOTO.\n"
    "OUTFIT: Black short-sleeve polo shirt. Classic polo collar with buttons. Clean, smooth "
    "fabric with natural texture. Shirt neatly fitted and wrinkle-free. Overall appearance "
    "simple, neat, and professional. No additional accessories or patterns.\n"
    "POSE: Straight upright posture. Shoulders relaxed and balanced. Arms naturally lowered. "
    "Neutral, formal ID photo expression. Body proportions unchanged, no stretching or "
    "distortion.\n"
    "COMPOSITION & CAMERA: Expand the framing to a waist-up portrait, including the subject "
    "from the waist upward while keeping the subject centered and maintaining natural body "
    "proportions. Front-facing angle. Camera positioned at eye level. Subject centered "
    "perfectly in the frame.\n"
    "BACKGROUND & LIGHTING: Replace the background with a solid pure white background "
    "(#FFFFFF). Background must be completely uniform, flat color. No gradients, no texture, "
    "no shadows, no vignetting, no lighting effects. Subject must remain naturally separated "
    "from the background. Do not alter facial lighting while changing the background.\n"
    "IMAGE QUALITY: Ultra high resolution. Sharp focus across entire subject. Natural skin "
    "texture, no artificial smoothing. Neutral color tones, balanced contrast. Fully "
    "photorealistic. Must not look AI-generated.\n"
    "EXCLUSIONS: Do not change the face. Do not change facial lighting. Do not recreate or "
    "retouch the face in any form. No cartoon, painting, CGI style. No AI-like appearance."
)

model_name = 'gemini-2.5-flash-image'
model = genai.GenerativeModel(model_name)

t0 = time.time()
try:
    response = model.generate_content(
        [img, MERGE_OUTFIT_PROMPT],
        generation_config={"response_modalities": ["IMAGE"]},
        request_options={'timeout': 60},
    )
    elapsed = time.time() - t0
    print(f"[Test] generate_content OK in {elapsed:.1f}s")
    print(f"[Test] candidates: {len(response.candidates)}")
    cand = response.candidates[0]
    print(f"[Test] finish_reason: {cand.finish_reason}")
    for i, part in enumerate(cand.content.parts):
        has_inline = hasattr(part, 'inline_data') and part.inline_data and part.inline_data.data
        text_val = getattr(part, 'text', None)
        print(f"  part[{i}]: inline_data={'yes, mime='+part.inline_data.mime_type+', bytes='+str(len(part.inline_data.data)) if has_inline else 'no'} text={text_val[:80] if text_val else None}")
        if has_inline:
            out_path = f'scratch/test_output_{i}.png'
            with open(out_path, 'wb') as f:
                f.write(part.inline_data.data)
            print(f"  -> saved to {out_path}")
except Exception as e:
    elapsed = time.time() - t0
    print(f"[Test] FAILED after {elapsed:.1f}s: {type(e).__name__}: {e}")
    raise
