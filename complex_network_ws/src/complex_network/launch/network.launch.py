r"""Bare-bones full topology, using ROS topics for every connection.

Robot 1 + gateway --\
                    Edge A -> Site link A --\
Robot 2 + gateway --/                        \
                                             Load balancer -> Workers 1, 2
Robot 3 + gateway --\                        /
                    Edge B -> Site link B --/
Robot 4 + gateway --/

Gateways/edges are pass-through placeholders, not REST implementations.
Site-link nodes represent the router positions; they are ROS relays, not IP routers.
"""
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    nodes = [
        DeclareLaunchArgument('video_enabled', default_value='true'),
        DeclareLaunchArgument('video_fps', default_value='5.0'),
        DeclareLaunchArgument('video_width', default_value='320'),
        DeclareLaunchArgument('video_height', default_value='180'),
        DeclareLaunchArgument('video_source', default_value='synthetic'),
    ]
    for site, robots in (('site_a', (1, 2)), ('site_b', (3, 4))):
        gateway_topics = []
        video_gateway_topics = []
        for number in robots:
            robot_id = f'robot_{number}'
            namespace = f'/{site}/{robot_id}'
            uplink = namespace + '/uplink'
            gateway_topics.append(uplink)
            video_uplink = namespace + '/video_uplink'
            video_gateway_topics.append(video_uplink)
            nodes.append(Node(
                package='complex_network', executable='robot', name='source',
                namespace=namespace, output='screen',
                parameters=[{'robot_id': robot_id, 'publish_rate_hz': 1.0}],
            ))
            nodes.append(Node(
                package='complex_network', executable='video', name='video',
                namespace=namespace, output='screen',
                condition=IfCondition(LaunchConfiguration('video_enabled')),
                parameters=[{'robot_id': robot_id,
                             'fps': ParameterValue(LaunchConfiguration('video_fps'), value_type=float),
                             'width': ParameterValue(LaunchConfiguration('video_width'), value_type=int),
                             'height': ParameterValue(LaunchConfiguration('video_height'), value_type=int),
                             'source': ParameterValue(LaunchConfiguration('video_source'), value_type=str)}],
            ))
            nodes.append(Node(
                package='complex_network', executable='relay', name='gateway',
                namespace=namespace, output='screen',
                parameters=[{'input_topics': [namespace + '/telemetry'],
                             'output_topic': uplink,
                             'video_input_topics': [namespace + '/camera/image/compressed'],
                             'video_output_topic': video_uplink}],
            ))
        nodes.append(Node(
            package='complex_network', executable='relay', name='edge',
            namespace='/' + site, output='screen',
            parameters=[{'input_topics': gateway_topics,
                         'output_topic': f'/{site}/edge/uplink',
                         'video_input_topics': video_gateway_topics,
                         'video_output_topic': f'/{site}/edge/video_uplink'}],
        ))
        nodes.append(Node(
            package='complex_network', executable='relay', name='site_link_stub',
            namespace='/' + site, output='screen',
            parameters=[{'input_topics': [f'/{site}/edge/uplink'],
                         'output_topic': '/central/ingress',
                         'video_input_topics': [f'/{site}/edge/video_uplink'],
                         'video_output_topic': '/central/video_ingress'}],
        ))

    nodes.append(Node(
        package='complex_network', executable='load_balancer',
        name='load_balancer', namespace='/central', output='screen',
    ))
    for number in (1, 2):
        nodes.append(Node(
            package='complex_network', executable='worker', name='worker',
            namespace=f'/central/worker_{number}', output='screen',
        ))
    return LaunchDescription(nodes)
