"""Small deterministic JPEG test-pattern clip; usable without importing ROS."""
from io import BytesIO
import random


def generate_jpeg_frames(width=320, height=180, frame_count=20, quality=80):
    """Return encoded moving frames. These images are synthetic, not a camera feed."""
    from PIL import Image, ImageDraw

    width, height, frame_count = int(width), int(height), int(frame_count)
    if width < 32 or height < 32 or frame_count < 1 or not 1 <= int(quality) <= 95:
        raise ValueError('Require width/height >= 32, frame_count >= 1 and quality 1..95')
    frames = []
    random_source = random.Random(61061)
    # Fixed detailed background makes JPEG payloads large enough for a network
    # workload. Seeded image noise is synthetic texture, not camera content.
    texture = Image.frombytes('RGB', (width, height),
                              random_source.randbytes(width * height * 3))
    background = Image.blend(Image.new('RGB', (width, height), (25, 35, 50)),
                             texture, 0.65)
    for index in range(frame_count):
        image = background.copy()
        draw = ImageDraw.Draw(image)
        for x in range(0, width, max(1, width // 8)):
            draw.line((x, 0, x, height - 1), fill=(70, 85, 100))
        for y in range(0, height, max(1, height // 6)):
            draw.line((0, y, width - 1, y), fill=(70, 85, 100))
        size = max(8, min(width, height) // 4)
        x = int((width - size) * index / max(1, frame_count - 1))
        y = (height - size) // 2
        draw.rectangle((x, y, x + size - 1, y + size - 1), fill=(245, 175, 45))
        draw.text((5, 5), 'SYNTHETIC VIDEO', fill='white')
        draw.text((5, height - 16), f'Frame {index:03d}', fill='white')
        buffer = BytesIO()
        image.save(buffer, format='JPEG', quality=int(quality))
        frames.append(buffer.getvalue())
    return frames
