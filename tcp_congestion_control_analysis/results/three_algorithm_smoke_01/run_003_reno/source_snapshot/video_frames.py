"""Load the project video as JPEG frames for the ROS camera topic."""

from pathlib import Path

import cv2


# Use this project video for every run; there is no alternative image source.
VIDEO_PATH = Path(__file__).resolve().parents[1] / "dependencies" / (
    "FPV Drone Flight through Beautiful Iceland Canyon - Joshua Turner (1080p).mp4"
)


def load_video_frames():
    # Decode before measurement starts so video decoding does not affect timings.
    if not VIDEO_PATH.is_file():
        raise FileNotFoundError(f"Project video is missing: {VIDEO_PATH}")
    capture = cv2.VideoCapture(str(VIDEO_PATH))
    frames = []
    try:
        if not capture.isOpened():
            raise RuntimeError(f"Cannot open project video: {VIDEO_PATH}")
        source_fps = capture.get(cv2.CAP_PROP_FPS)
        if source_fps <= 0:
            raise RuntimeError("The project video has no valid frame rate")

        # Sample the whole clip at five frames per second, matching the camera rate.
        # Keep the previous 320 x 180 size and JPEG quality to control payload size.
        index, next_frame = 0, 0.0
        while capture.grab():
            if index >= next_frame:
                ok, image = capture.retrieve()
                if not ok:
                    raise RuntimeError("Cannot decode a frame from the project video")
                image = cv2.resize(image, (320, 180), interpolation=cv2.INTER_AREA)
                ok, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if not ok:
                    raise RuntimeError("Cannot encode a project video frame as JPEG")
                frames.append(jpeg.tobytes())
                next_frame += source_fps / 5
            index += 1
    finally:
        # Always release the decoder, including when a frame fails to decode.
        capture.release()

    if not frames:
        raise RuntimeError("The project video contains no readable frames")
    return frames
