"""ROS 2 compressed-video source: synthetic pattern, camera index, or video file."""
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage

from .video_frames import generate_jpeg_frames


class VideoSource(Node):
    def __init__(self):
        super().__init__('video')
        self.robot_id = self.declare_parameter('robot_id', 'robot_1').value
        fps = float(self.declare_parameter('fps', 5.0).value)
        width = int(self.declare_parameter('width', 320).value)
        height = int(self.declare_parameter('height', 180).value)
        self.quality = int(self.declare_parameter('jpeg_quality', 80).value)
        source = str(self.declare_parameter('source', 'synthetic').value)
        if fps <= 0:
            raise ValueError('fps must be positive')
        self.capture = None
        self.sequence = 0
        self.frames = None
        if source == 'synthetic':
            self.frames = generate_jpeg_frames(width, height, quality=self.quality)
        else:
            try:
                import cv2
            except ImportError as exc:
                raise RuntimeError('Camera/file sources require python3-opencv') from exc
            self.cv2 = cv2
            self.capture = cv2.VideoCapture(int(source) if source.isdigit() else source)
            self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            if not self.capture.isOpened():
                raise RuntimeError(f'Cannot open video source: {source}')
        self.publisher = self.create_publisher(CompressedImage, 'camera/image/compressed', 10)
        self.timer = self.create_timer(1.0 / fps, self.publish_frame)
        self.get_logger().info(f'{self.robot_id}: {source} JPEG stream, {fps:g} frames/s')

    def publish_frame(self):
        if self.capture is None:
            data = self.frames[self.sequence % len(self.frames)]
        else:
            ok, frame = self.capture.read()
            if not ok:
                self.capture.set(self.cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = self.capture.read()
            if not ok:
                self.get_logger().error('Video source returned no frame')
                return
            ok, encoded = self.cv2.imencode('.jpg', frame,
                                           [self.cv2.IMWRITE_JPEG_QUALITY, self.quality])
            if not ok:
                self.get_logger().error('JPEG encoding failed')
                return
            data = encoded.tobytes()
        message = CompressedImage()
        message.header.stamp = self.get_clock().now().to_msg()
        message.header.frame_id = f'{self.robot_id}/camera/{self.sequence}'
        message.format = 'jpeg'
        message.data = data
        self.publisher.publish(message)
        self.sequence += 1

    def destroy_node(self):
        if self.capture is not None:
            self.capture.release()
        return super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = VideoSource()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
