"""ROS-only placeholders; gateway, edge and link behaviour can be replaced."""
import json

import rclpy
from rclpy.node import Node
from std_msgs.msg import String


class Robot(Node):
    def __init__(self):
        super().__init__('robot')
        self.robot_id = self.declare_parameter('robot_id', 'robot_1').value
        rate = self.declare_parameter('publish_rate_hz', 1.0).value
        if rate <= 0:
            raise ValueError('publish_rate_hz must be positive')
        self.publisher = self.create_publisher(String, 'telemetry', 10)
        self.sequence = 0
        self.timer = self.create_timer(1.0 / rate, self.publish_telemetry)

    def publish_telemetry(self):
        message = String()
        message.data = json.dumps({'robot_id': self.robot_id,
                                   'sequence': self.sequence})
        self.publisher.publish(message)
        self.sequence += 1


class Relay(Node):
    """Pass-through stub for a robot gateway, site edge or site link."""
    def __init__(self):
        super().__init__('relay')
        inputs = self.declare_parameter('input_topics', ['input']).value
        output = self.declare_parameter('output_topic', 'output').value
        self.publisher = self.create_publisher(String, output, 10)
        self.inputs = [self.create_subscription(String, topic, self.forward, 10)
                       for topic in inputs]

    def forward(self, message):
        self.publisher.publish(message)


class LoadBalancer(Node):
    """Minimal round-robin dispatch to the two worker input topics."""
    def __init__(self):
        super().__init__('load_balancer')
        outputs = self.declare_parameter('worker_topics',
                                        ['worker_1/input', 'worker_2/input']).value
        self.publishers_to_workers = [self.create_publisher(String, topic, 10)
                                      for topic in outputs]
        self.next_worker = 0
        self.input = self.create_subscription(String, 'ingress', self.dispatch, 10)

    def dispatch(self, message):
        self.publishers_to_workers[self.next_worker].publish(message)
        self.next_worker = (self.next_worker + 1) % len(self.publishers_to_workers)


class Worker(Node):
    """Processing placeholder; emits the unchanged message on its result topic."""
    def __init__(self):
        super().__init__('worker')
        self.publisher = self.create_publisher(String, 'result', 10)
        self.input = self.create_subscription(String, 'input', self.process, 10)

    def process(self, message):
        # Insert central processing here.
        self.publisher.publish(message)


def spin(node_type, args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = node_type()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


def robot_main(args=None):
    spin(Robot, args)


def relay_main(args=None):
    spin(Relay, args)


def balancer_main(args=None):
    spin(LoadBalancer, args)


def worker_main(args=None):
    spin(Worker, args)
